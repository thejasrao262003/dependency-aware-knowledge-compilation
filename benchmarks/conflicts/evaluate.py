"""Precision and recall of conflict detection on labelled sentence pairs.

    python benchmarks/conflicts/evaluate.py                     # rules only, free
    python benchmarks/conflicts/evaluate.py --json out.json
    python benchmarks/conflicts/evaluate.py --provider-config provider.toml   # + the verifier: model calls, paid

``provider.toml`` is the format of ``benchmarks/llm_experiment.py`` (``provider = "openai"``
and an ``[options]`` table). With it, every pair the rules do not flag and that would
reach the verifier in a compile (the statements share an entity and come from different
documents) is judged by that provider's verifier, ten pairs per call.

Each line of ``pairs-*.jsonl`` is two statements (text, file path, heading) labelled
``contradiction`` (cannot both be true in the same scope), ``scoped`` (they differ, but
the text, heading or path says they apply to different releases, environments or
statuses) or ``compatible`` (both can be true). ``pairs-dev.jsonl`` was written while
the rules were designed; ``pairs-test.jsonl`` was written separately, without access to
the detector, and is the number to quote.

A pair counts as **flagged** when detection over the two statements reports an open
conflict between them (with the verifier: a ``possible`` conflict, after DAKC's own scope
reading of headings and paths). Contradictions should be flagged; scoped and compatible
pairs should not. Each statement goes through the same heuristic extractor as a compile.
**Reach** is the share of contradictions the verifier would be shown at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from dakc.conflicts import Occurrence, detect, index_rows
from dakc.conflicts.verifier import findings as verifier_findings
from dakc.conflicts.verifier import subject_key
from dakc.hashing import claim_id
from dakc.providers.deterministic import HeuristicExtractor

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # the repository root, for benchmarks.llm_experiment
EX = HeuristicExtractor()


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def occurrences(side: dict, tag: str) -> list[tuple[Occurrence, tuple[str, ...], str]]:
    return [(Occurrence(claim_id(c.text), f"psg:{tag}", side["path"], side["heading"], c.text), tuple(c.mentions), c.text)
            for c in EX.extract(side["text"]).claims]


def rules_verdict(pair: dict) -> tuple[str, bool]:
    """(verdict, extracted): 'conflict', 'scoped' or 'none' for the pair."""
    a, b = occurrences(pair["a"], "a"), occurrences(pair["b"], "b")
    found = detect(a + b)
    kinds = set()
    for f in found.values():
        sides = {o["passage_id"] for v in f.values for o in v["occurrences"]}
        if sides == {"psg:a", "psg:b"}:
            kinds.add(f.kind)
    verdict = "conflict" if "conflict" in kinds else "scoped" if "scoped" in kinds else "none"
    return verdict, bool(a and b)


def reaches_verifier(pair: dict) -> bool:
    """Would a compile hand this pair to the verifier? Different documents, a shared entity,
    and no property key the rules already compare."""
    a, b = occurrences(pair["a"], "a"), occurrences(pair["b"], "b")
    if not a or not b or pair["a"]["path"] == pair["b"]["path"]:
        return False
    ents = lambda occ: {subject_key(m) for _, ms, _ in occ for m in ms}  # noqa: E731
    keys = lambda occ: {r[0] for r in index_rows(occ)}  # noqa: E731
    return bool(ents(a) & ents(b)) and not (keys(a) & keys(b))


def verifier_verdicts(pairs: list[dict], bundle, store: Path | None = None) -> tuple[dict[str, str], dict[str, list[str]]]:
    """(pair id -> 'conflict' | 'scoped' | 'none', pair id -> [model verdict, reason]) for the
    pairs that reach the verifier. ``store``: a JSON file of verdicts already paid for, by
    verifier fingerprint and pair texts; only pairs missing from it are sent to the model."""
    V = bundle.verifier
    known = json.loads(store.read_text(encoding="utf-8")) if store is not None and store.exists() else {}
    todo = [p for p in pairs if reaches_verifier(p)]
    texts = {p["id"]: (occurrences(p["a"], "a")[0][2], occurrences(p["b"], "b")[0][2]) for p in todo}
    key = lambda pid: "|".join((V.fingerprint, *texts[pid]))  # noqa: E731
    ask = [p for p in todo if key(p["id"]) not in known]
    usage = [0, 0, 0]
    for i in range(0, len(ask), 10):
        batch = ask[i:i + 10]
        res = V.judge([texts[p["id"]] for p in batch])
        usage[0] += 1
        usage[1] += res.input_tokens
        usage[2] += res.output_tokens
        for p, vr in zip(batch, res.verdicts):
            if vr[0]:
                known[key(p["id"])] = list(vr)
    if store is not None and ask:
        store.write_text(json.dumps(known, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    out: dict[str, str] = {}
    raw: dict[str, list[str]] = {}
    for p in todo:
        verdict, reason = known.get(key(p["id"]), ["", ""])
        raw[p["id"]] = [verdict, reason]
        occ = {"a": occurrences(p["a"], "a"), "b": occurrences(p["b"], "b")}
        ca, cb = occ["a"][0][0].claim_id, occ["b"][0][0].claim_id
        found = verifier_findings([{"a": ca, "b": cb, "verdict": verdict, "reason": reason}],
                                  lambda cid: [o for side in occ.values() for o in side if o[0].claim_id == cid],
                                  lambda x, y: [])
        kinds = {f.kind for _, f in found.values()}
        out[p["id"]] = "conflict" if "possible" in kinds else "scoped" if "scoped" in kinds else "none"
    print(f"  verifier: {len(todo) - len(ask)} pairs judged before, {len(ask)} sent in {usage[0]} calls, "
          f"{usage[1]:,} input and {usage[2]:,} output tokens")
    return out, raw


def evaluate(pairs: list[dict], bundle=None, store: Path | None = None) -> dict:
    by_cat: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])  # (label, category) -> [n, flagged]
    rows = []
    judged, raw = verifier_verdicts(pairs, bundle, store) if bundle is not None else ({}, {})
    reach = [0, 0]
    for p in pairs:
        verdict, extracted = rules_verdict(p)
        if verdict == "none":  # the verifier only sees pairs the rules do not compare
            verdict = judged.get(p["id"], "none")
        flagged = verdict == "conflict"
        by_cat[(p["label"], p["category"])][0] += 1
        by_cat[(p["label"], p["category"])][1] += flagged
        if p["label"] == "contradiction":
            reach[1] += 1
            reach[0] += rules_verdict(p)[0] == "conflict" or reaches_verifier(p)
        rows.append({"id": p["id"], "label": p["label"], "category": p["category"], "verdict": verdict,
                     "extracted": extracted, "reaches_verifier": reaches_verifier(p),
                     **({"model": raw[p["id"]]} if p["id"] in raw else {})})
    lab = defaultdict(lambda: [0, 0])
    for (label, _), (n, f) in by_cat.items():
        lab[label][0] += n
        lab[label][1] += f
    tp = lab["contradiction"][1]
    fp = lab["scoped"][1] + lab["compatible"][1]
    return {
        "pairs": len(pairs),
        "verifier": bundle.verifier.fingerprint if bundle is not None else None,
        "recall": [tp, lab["contradiction"][0]],
        "false_flags": [fp, lab["scoped"][0] + lab["compatible"][0]],
        "precision": [tp, tp + fp],
        "contradictions_rules_or_verifier_can_see": reach,
        "by_category": [{"label": k[0], "category": k[1], "n": v[0], "flagged": v[1]} for k, v in sorted(by_cat.items())],
        "rows": rows,
    }


def pct(x: list[int]) -> str:
    return f"{x[0]}/{x[1]} ({100 * x[0] / x[1]:.0f}%)" if x[1] else "n/a"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sets", nargs="*", default=["dev", "test"])
    ap.add_argument("--json", type=Path)
    ap.add_argument("--provider-config", type=Path, help="judge pairs with this provider's verifier (model calls, paid)")
    ap.add_argument("--verdicts", type=Path, help="JSON file of verdicts already paid for: reused, and new ones added")
    args = ap.parse_args()
    bundle = None
    if args.provider_config:
        from benchmarks.llm_experiment import load_bundle

        bundle = load_bundle(args.provider_config)
    out = {}
    for name in args.sets:
        path = HERE / f"pairs-{name}.jsonl"
        if not path.exists():
            print(f"{name}: {path.name} not found, skipped")
            continue
        print(f"\n== {name}" + (f" (rules + {bundle.verifier.fingerprint})" if bundle else " (rules)"))
        r = evaluate(load(path), bundle, args.verdicts)
        out[name] = r
        print(f"  {r['pairs']} pairs | recall {pct(r['recall'])} | precision {pct(r['precision'])} | "
              f"false flags {pct(r['false_flags'])} | contradictions the rules or the verifier would see "
              f"{pct(r['contradictions_rules_or_verifier_can_see'])}")
        for c in r["by_category"]:
            print(f"  {c['label']:14} {c['category']:26} flagged {c['flagged']:>3}/{c['n']}")
    if args.json:
        args.json.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
