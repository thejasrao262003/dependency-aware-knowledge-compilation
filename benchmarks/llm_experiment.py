"""Real-LLM experiments (optional; costs provider calls).

Part A: does an edit to one sentence leave the other facts of the paragraph
    untouched? For each paragraph (>= 3 sentences) the claims are extracted
    (v1), the paragraph is re-extracted unchanged (repeat stability), then one
    fact is edited (an entity in one sentence replaced by an unseen one) and the
    new version is processed two ways:
      * passage:  the whole paragraph is re-extracted (DAKC's original behaviour);
      * sentence: claims of unchanged sentences are reused verbatim and only the
                  changed / context-dependent sentences are sent to the model
                  (dakc.claims.plan_reuse, the current behaviour).
    Measured: unchanged-fact reproduction (same claim ids), stale retention (an
    old claim still stating the replaced detail), new-fact capture, tokens.
    Paragraph sets: synthetic, code-heavy technical notes, explanatory prose,
    and a coreference set in which the edited sentence is the antecedent of a
    pronoun in the next sentence (the specific risk of sentence reuse).

Part B: end-to-end incremental compilation with the real provider on a small
    synthetic corpus: full build cost, then per scenario the file-level
    baseline, DAKC with whole-passage re-extraction, and DAKC with sentence
    reuse, with provider-reported tokens, invariant checks and, for the
    edited-value scenario, a direct check that the old value is gone.

Usage:
    python -m benchmarks.llm_experiment --provider-config path/to/provider.toml --out benchmarks/results/llm.json

provider.toml:
    provider = "openai"            # or anthropic / custom
    [options]                      # passed to dakc.providers.make_provider
    base_url = "http://localhost:11434/v1"
    model = "llama3.1"
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rapidfuzz import fuzz

from benchmarks.corpus import apply_scenario, generate
from benchmarks.run import machine
from dakc.claims import extract_mentions, plan_reuse, sentence_key, split_sentences
from dakc.compiler import Compiler
from dakc.hashing import claim_id, normalize_claim
from dakc.parsing import parse_bytes
from dakc.providers import ProviderBundle, make_provider
from dakc.provenance.trace import citation_coverage
from dakc.provenance.verify import verify_store
from dakc.storage import Store

ROOT = Path(__file__).resolve().parents[1]
NEW_ENTITY = "Atlasbridge"
MODES = ("passage", "sentence", "sentence-evidence-only")
E2E_SCENARIOS = ("S0_no_change", "S1_one_modified", "S3_one_deleted", "S4_one_renamed", "S5_one_claim_modified", "S8_many_pages")


def load_bundle(config: Path) -> ProviderBundle:
    cfg = tomllib.loads(config.read_text(encoding="utf-8"))
    options = dict(cfg.get("options", {}))
    if cfg["provider"] == "custom" and options.get("path"):
        options["path"] = str((config.parent / options["path"]).resolve()) if not Path(options["path"]).is_absolute() else options["path"]
    return make_provider(cfg["provider"], options)


# ----------------------------------------------------------------- Part A data
def synthetic_paragraphs(n: int, work: Path) -> list[str]:
    generate(work / "para-corpus", 300, seed=21)
    paras = []
    for p in sorted((work / "para-corpus").rglob("*.md")):
        for b in parse_bytes(p.read_bytes(), "markdown").body_blocks:
            if len(split_sentences(b.text)) >= 3:
                paras.append(b.text)
    random.Random(5).shuffle(paras)
    return list(dict.fromkeys(paras))[:n]


def _paragraphs_from(files: list[Path], n: int) -> list[str]:
    paras = []
    for p in files:
        for b in parse_bytes(p.read_bytes(), "markdown").body_blocks:
            sents = split_sentences(b.text)
            if 3 <= len(sents) <= 6 and 150 <= len(b.text) <= 700 and edit_paragraph(b.text) is not None:
                paras.append(b.text)
    random.Random(5).shuffle(paras)
    return list(dict.fromkeys(paras))[:n]


def prose_paragraphs(n: int) -> list[str]:
    """Explanatory prose: this repository's design documents."""
    files = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")), *sorted((ROOT / "research").glob("*.md"))]
    return _paragraphs_from([f for f in files if f.name != "benchmark.md"], n)


COREF_TEMPLATES = [
    ("{a} is the main {thing} behind the {team} service.", "It {rel} {b}.", "{c} {rel2} {d}."),
    ("The {team} team migrated its workloads to {a}.", "Since then the platform {rel} {b}.", "{c} {rel2} {d}."),
    ("{a} replaced the old {thing} last year.", "This change means the stack now {rel} {b}.", "{c} {rel2} {d}."),
]


def coreference_paragraphs(n: int) -> list[tuple[str, str]]:
    """(paragraph, antecedent entity): sentence 2 depends on sentence 1 through a pronoun or 'the X'."""
    rng = random.Random(9)
    names = ["Nexiodyn", "Kelitron", "Draildock", "Sylabase", "Halecast", "Morogrid", "Quorlite", "Ulosql", "Feniovault", "Jorohive"]
    things = ["database", "queue", "cache", "scheduler", "gateway"]
    teams = ["Billing", "Ledger", "Search", "Identity", "Payments"]
    rels = ["depends on", "streams data to", "authenticates against", "replicates to"]
    out = []
    for i in range(n):
        a, b, c, d = rng.sample(names, 4)
        t1, t2, t3 = COREF_TEMPLATES[i % len(COREF_TEMPLATES)]
        para = " ".join([
            t1.format(a=a, thing=rng.choice(things), team=rng.choice(teams)),
            t2.format(rel=rng.choice(rels), b=b),
            t3.format(c=c, rel2=rng.choice(rels), d=d),
        ])
        out.append((para, a))
    return out


def technical_paragraphs(n: int) -> list[str]:
    """Code-heavy technical notes: the audit notes in research/audits (independent of the synthetic generator)."""
    paras = []
    for p in sorted((ROOT / "research" / "audits").glob("*.md")):
        for b in parse_bytes(p.read_bytes(), "markdown").body_blocks:
            sents = split_sentences(b.text)
            if 3 <= len(sents) <= 6 and 150 <= len(b.text) <= 700 and any(extract_mentions(s) for s in sents[1:-1] or sents):
                paras.append(b.text)
    random.Random(5).shuffle(paras)
    return list(dict.fromkeys(paras))[:n]


def edit_paragraph(text: str, sentence: int | None = None, target: str | None = None) -> tuple[str, int, str] | None:
    """Replace the last entity mention in the middle-most sentence that has one
    (or ``target`` in sentence ``sentence`` when given)."""
    if sentence is not None and target is not None:
        sents = split_sentences(text)
        k = sents[sentence].find(target)
        if k < 0:
            return None
        out = list(sents)
        out[sentence] = sents[sentence][:k] + NEW_ENTITY + sents[sentence][k + len(target):]
        return " ".join(out), sentence, target
    sents = split_sentences(text)
    order = sorted(range(len(sents)), key=lambda i: abs(i - len(sents) // 2))
    for j in order:
        # Only entities that are not sentence-initial: replacing the subject word
        # of natural prose ("Errors that ...") garbles the sentence instead of
        # changing one fact.
        mentions = [m for m in extract_mentions(sents[j])
                    if sents[j].rfind(m) > 0 and text.casefold().count(m.casefold()) == 1]  # unambiguous edit
        if not mentions:
            continue
        target = max(mentions, key=lambda m: sents[j].rfind(m))
        k = sents[j].rfind(target)
        new = sents[j][:k] + NEW_ENTITY + sents[j][k + len(target):]
        if new != sents[j]:
            out = list(sents)
            out[j] = new
            return " ".join(out), j, target
    return None


def assign(claim_text: str, sents: list[str]) -> int:
    nc = normalize_claim(claim_text)
    scores = [fuzz.token_sort_ratio(nc, normalize_claim(s)) for s in sents]
    return max(range(len(sents)), key=lambda i: scores[i])


def part_a(config: Path, paragraphs: dict[str, list], jobs: int, log, work: Path) -> dict:
    ex = load_bundle(config).extractor
    out: dict = {}
    for corpus_name, paras in paragraphs.items():
        items = []
        for entry in paras:
            if isinstance(entry, tuple):  # (paragraph, antecedent) from the coreference set
                para, antecedent = entry
                e = edit_paragraph(para, sentence=0, target=antecedent)
            else:
                para, e = entry, edit_paragraph(entry)
            if e is not None:
                items.append((para, e))

        def run_one(item):
            p, (edited, j, target) = item
            try:
                v1 = ex.extract(p)
                rep = ex.extract(p)
                v2_passage = ex.extract(edited)
                psents = split_sentences(p)
                prior = [(c.text, c.mentions, tuple(sentence_key(psents[i]) for i in c.evidence if i < len(psents))) for c in v1.claims]
                v2s, dirty_by_mode = {}, {}
                for mode, rule in (("sentence", True), ("sentence-evidence-only", False)):
                    reused, dirty = plan_reuse(prior, {sentence_key(x) for x in psents}, edited, context_rule=rule)
                    dirty_by_mode[mode] = dirty
                    if dirty:
                        fresh = ex.extract(edited, tuple(dirty))
                        seen = {claim_id(c.text) for c in reused}
                        sclaims = reused + [c for c in fresh.claims if claim_id(c.text) not in seen]
                        v2s[mode] = type(fresh)(sclaims, fresh.input_tokens, fresh.output_tokens, fresh.calls)
                    else:
                        v2s[mode] = type(v1)(reused, 0, 0, 0)
            except Exception as exc:  # noqa: BLE001 - recorded, paragraph excluded
                return f"{type(exc).__name__}: {str(exc)[:120]}"
            return p, edited, j, target, v1, rep, {"passage": v2_passage, **v2s}, dirty_by_mode

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            raw = list(pool.map(run_one, items))
        failures = [r for r in raw if isinstance(r, str)]
        results = [r for r in raw if not isinstance(r, str)]

        agg = {"paragraphs": len(results), "paragraphs_failed": len(failures), "v1_claims": 0, "unchanged_claims": 0, "edited_claims": 0,
               "repeat_reproduced": 0, "tokens_in": 0, "tokens_out": 0, "calls": 0}
        per_mode = {m: {"unchanged_reproduced": 0, "stale_retained": 0, "edited_survived": 0, "new_fact_captured": 0,
                        "tokens_in": 0, "tokens_out": 0, "calls": 0} for m in MODES}
        examples, raw_records = [], []
        agg["detail_claims"] = 0
        agg["failures"] = failures[:10]
        agg["sentences_sent"] = {m: 0 for m in MODES if m != "passage"}
        agg["sentences_total"] = 0
        for p, edited, j, target, v1, rep, v2, dirty_by_mode in results:
            sents = split_sentences(p)
            agg["sentences_total"] += len(sents)
            for m, d in dirty_by_mode.items():
                agg["sentences_sent"][m] += len(d)
            ids_v1 = [claim_id(c.text) for c in v1.claims]
            groups = [assign(c.text, sents) for c in v1.claims]
            # The replaced entity occurs exactly once in the paragraph (edit_paragraph guarantees it), so every
            # old claim naming it derives from the edited sentence, directly or through a pronoun.
            detail_ids = {claim_id(c.text) for c in v1.claims if target.casefold() in c.text.casefold()}
            unchanged = {i for i, g in zip(ids_v1, groups) if g != j} - detail_ids
            edited_ids = {i for i, g in zip(ids_v1, groups) if g == j} - unchanged
            agg["v1_claims"] += len(set(ids_v1))
            agg["unchanged_claims"] += len(unchanged)
            agg["edited_claims"] += len(edited_ids)
            agg["detail_claims"] += len(detail_ids)
            raw_records.append({"paragraph": p, "edited": edited, "sentence": j, "old_detail": target, "dirty": dirty_by_mode,
                                "v1": [[c.text, list(c.evidence)] for c in v1.claims], "repeat": [c.text for c in rep.claims],
                                **{f"v2_{m}": [c.text for c in r.claims] for m, r in v2.items()}})
            agg["repeat_reproduced"] += len(set(ids_v1) & {claim_id(c.text) for c in rep.claims})
            for r in [v1, rep, *v2.values()]:
                agg["tokens_in"] += r.input_tokens
                agg["tokens_out"] += r.output_tokens
                agg["calls"] += r.calls
            for m, r in v2.items():
                per_mode[m]["tokens_in"] += r.input_tokens
                per_mode[m]["tokens_out"] += r.output_tokens
                per_mode[m]["calls"] += r.calls
                ids_v2 = {claim_id(c.text) for c in r.claims}
                per_mode[m]["unchanged_reproduced"] += len(unchanged & ids_v2)
                per_mode[m]["stale_retained"] += len(detail_ids & ids_v2)
                per_mode[m]["edited_survived"] += len(edited_ids & ids_v2)
                per_mode[m]["new_fact_captured"] += int(any(NEW_ENTITY.casefold() in c.text.casefold() for c in r.claims))
                if detail_ids & ids_v2 and len(examples) < 6:
                    examples.append({"mode": m, "edited_sentence": split_sentences(edited)[j],
                                     "stale_claim_kept": [c.text for c in v1.claims if claim_id(c.text) in detail_ids & ids_v2]})
        summary = {
            **agg,
            "repeat_stability": round(agg["repeat_reproduced"] / max(1, agg["v1_claims"]), 4),
            "modes": {
                m: {
                    "unchanged_fact_reproduction": round(v["unchanged_reproduced"] / max(1, agg["unchanged_claims"]), 4),
                    "stale_retention": round(v["stale_retained"] / max(1, agg["detail_claims"]), 4),
                    "stale_claims": v["stale_retained"],
                    "edited_sentence_claims_unchanged": round(v["edited_survived"] / max(1, agg["edited_claims"]), 4),
                    "tokens_in": v["tokens_in"],
                    "tokens_out": v["tokens_out"],
                    "calls": v["calls"],
                    "new_fact_capture": round(v["new_fact_captured"] / max(1, agg["paragraphs"]), 4),
                }
                for m, v in per_mode.items()
            },
            "stale_examples": examples,
        }
        (Path(work) / f"part_a_raw_{corpus_name}.json").write_text(json.dumps(raw_records, indent=1), encoding="utf-8")
        out[corpus_name] = summary
        log(f"[A:{corpus_name}] {json.dumps(summary['modes'])} repeat={summary['repeat_stability']}")
    return out


# ----------------------------------------------------------------- Part B
def s5_value_check(store: Store, change: dict) -> dict:
    m = re.match(r"(.+?): (.+) -> (.+)$", change.get("claim", ""))
    if not m:
        return {}
    attr, old, new = (normalize_claim(x) for x in m.groups())
    texts = [normalize_claim(c.text) for c in store.load_state().of("claim").values()]
    doc = change["modified"][0]
    srcs = [s for s in store.load_sources().values() if s.path == doc]
    sid = srcs[0].id if srcs else None
    state = store.load_state()
    doc_claims = [normalize_claim(c.text) for c in state.of("claim").values()
                  if any(state.of("passage")[p].source_id == sid for p in c.passage_ids if p in state.of("passage"))]
    return {
        "old_value_still_claimed_by_doc": any(f"{attr} {old}" in t for t in doc_claims),
        "new_value_claimed": any(f"{attr} {new}" in t for t in texts),
    }


def part_b(config: Path, n_docs: int, jobs: int, work: Path, log) -> dict:
    wd = work / "e2e"
    shutil.rmtree(wd, ignore_errors=True)
    wd.mkdir(parents=True)
    generate(wd / "corpus", n_docs, seed=7)
    manifest = json.loads((wd / "corpus.manifest.json").read_text())
    store = Store(wd / "A.db")
    t0 = time.perf_counter()
    rep_a, _ = Compiler(store, load_bundle(config), wd / "corpus", strategy="full", jobs=jobs).run()
    full = {"seconds": round(time.perf_counter() - t0, 2), **rep_a.usage.as_dict(), "counts": store.counts(),
            "citation_coverage": citation_coverage(store)}
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store.close()
    log(f"[B] full build: {full['llm_calls']} calls, {full['input_tokens']}/{full['output_tokens']} tokens, {full['seconds']}s")
    out = {"n_docs": n_docs, "full_build": full, "scenarios": {}}
    runs = [("file", "file", False), ("dakc-passage", "dakc", False), ("dakc", "dakc", True)]
    for scenario in E2E_SCENARIOS:
        corpus = wd / f"c-{scenario}"
        shutil.copytree(wd / "corpus", corpus)
        change = apply_scenario(corpus, manifest, scenario)
        sc = {"change": change, "runs": {}}
        for label, strategy, sentence_reuse in runs:
            db = wd / "I.db"
            shutil.copy(wd / "A.db", db)
            st = Store(db)
            t0 = time.perf_counter()
            try:
                rep, _ = Compiler(st, load_bundle(config), corpus, strategy=strategy, jobs=jobs, sentence_reuse=sentence_reuse).run()
            except Exception as exc:  # noqa: BLE001 - a failed build leaves the store unchanged; record it
                sc["runs"][label] = {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:120]}", "invariants_pass": verify_store(st).ok}
                st.close()
                db.unlink()
                continue
            elapsed = time.perf_counter() - t0
            v = verify_store(st)
            row = {
                "llm_calls": rep.usage.llm_calls,
                "input_tokens": rep.usage.input_tokens,
                "output_tokens": rep.usage.output_tokens,
                "seconds": round(elapsed, 2),
                "passages_reextracted": rep.usage.by_stage.get("extract", 0),
                "pages_regenerated": rep.n("regenerate_page"),
                "pages_rerendered_no_llm": rep.n("rerender_page"),
                "pages_cutoff": rep.n("cutoff_page"),
                "summaries_regenerated": rep.n("regenerate_summary"),
                "sentences_reused": rep.counts.get("sentences_reused", 0),
                "sentences_extracted": rep.counts.get("sentences_extracted", 0),
                "claims_new": rep.n("new_claim"),
                "claims_retracted": rep.n("retract_claim"),
                "claims_reproduced_or_provenance_only": rep.n("claim_reproduced") + rep.n("claim_provenance_changed"),
                "invariants_pass": v.ok,
                "status": rep.status,
            }
            if scenario == "S5_one_claim_modified":
                row.update(s5_value_check(st, change))
            sc["runs"][label] = row
            st.close()
            db.unlink()
        out["scenarios"][scenario] = sc
        log(f"[B] {scenario}: " + ", ".join(f"{k}={r.get('llm_calls', 'FAIL')}c/{r.get('pages_regenerated', '-')}p" for k, r in sc["runs"].items()))
        shutil.rmtree(corpus)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider-config", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--paragraphs", type=int, default=40)
    ap.add_argument("--docs", type=int, default=30)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--work", type=Path, default=ROOT / "bench-work" / "llm")
    ap.add_argument("--parts", default="A,B")
    args = ap.parse_args()
    log = lambda m: print(m, flush=True)  # noqa: E731
    bundle = load_bundle(args.provider_config)
    result = {
        "machine": {**machine(), "provider": f"{bundle.name}: {bundle.extractor.client.fingerprint}"},
        "fingerprints": bundle.fingerprints(),
    }
    args.work.mkdir(parents=True, exist_ok=True)
    if "A" in args.parts:
        paras = {
            "synthetic": synthetic_paragraphs(args.paragraphs, args.work),
            "technical": technical_paragraphs(args.paragraphs),
            "prose": prose_paragraphs(args.paragraphs),
            "coreference": coreference_paragraphs(args.paragraphs),
        }
        result["part_a"] = part_a(args.provider_config, paras, args.jobs, log, args.work)
    if "B" in args.parts:
        result["part_b"] = part_b(args.provider_config, args.docs, args.jobs, args.work, log)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.exists():
        previous = json.loads(args.out.read_text(encoding="utf-8"))
        by_part = previous.get("machine_by_part") or {k: previous["machine"] for k in ("part_a", "part_b") if k in previous}
        by_part.update({k: result["machine"] for k in ("part_a", "part_b") if k in result})
        result = {**previous, **result, "machine_by_part": by_part}
    args.out.write_text(json.dumps(result, indent=1, sort_keys=True), encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
