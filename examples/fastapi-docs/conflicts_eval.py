"""Measure conflict detection (RFC 0002) on FastAPI's documentation.

    python examples/fastapi-docs/conflicts_eval.py      # writes results/conflicts.json and .md

1. **Precision.** Compile FastAPI's English docs (pinned commit, deterministic mock
   provider) and count findings. Every property stated by more than one claim is printed,
   so the 0 can be checked by reading them.
2. **Recall.** Take sentences from the same docs, negate them or change a single-valued
   value, extract the new sentence with the same extractor, and check that detection over
   the corpus flags it against the original. Fixed seed.
3. **Cost.** Time the conflict step on a one-file edit, and when everything is re-judged.

Nothing is sent to a model.
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from replay import DOCS, END, ensure_clone  # noqa: E402

import dakc  # noqa: E402
from dakc.compiler import Compiler  # noqa: E402
from dakc.conflicts import Occurrence, detect, index_rows, parse  # noqa: E402
from dakc.hashing import claim_id  # noqa: E402
from dakc.providers.deterministic import HeuristicExtractor  # noqa: E402

PLANTS = 150  # per kind
SEED = 11


def occurrences(store) -> list[tuple[Occurrence, tuple[str, ...], str]]:
    st = store.load_state()
    psg = st.of("passage")
    return [(Occurrence(c.id, pid, st.sources[psg[pid].source_id].path, psg[pid].heading, surface), tuple(m), c.text)
            for c in st.of("claim").values() for pid, surface, m, _ in c.supports]


def negate(sentence: str) -> str | None:
    for a, b in ((" is ", " is not "), (" are ", " are not "), (" can ", " can not "), (" will ", " will not ")):
        if a in sentence and " not " not in sentence:
            return sentence.replace(a, b, 1)
    m = re.match(r"^([A-Z][\w.\-]*) (\w+?)s (.+)$", sentence)
    return f"{m.group(1)} does not {m.group(2)} {m.group(3)}" if m else None


def revalue(occ: Occurrence, mentions: tuple[str, ...]) -> str | None:
    for _frame, value, functional in parse(occ, mentions).frames:
        if functional:
            if value.isdigit():
                return occ.sentence.replace(value, str(int(value) * 2 + 7), 1)
            idx = occ.sentence.lower().rfind(value.split()[0])
            if idx > 0:
                return occ.sentence[:idx] + "Blorptech."
    return None


def plain(occ: Occurrence, mentions: tuple[str, ...]) -> bool:
    """A statement of fact, not a list fragment, an instruction or a conditional."""
    t = occ.sentence.strip().lower()
    return parse(occ, mentions).scope.status == "current" and not t.startswith(("you ", "if ", "when ", "we ", "note"))


def recall(base) -> dict[str, dict[str, list[int]]]:
    ex = HeuristicExtractor()
    pool = [b for b in base if b[0].path != "release-notes.md" and len(b[0].sentence) < 160]
    random.Random(SEED).shuffle(pool)
    out = {}
    for kind, make in (("negation", lambda o, m: negate(o.sentence)), ("changed value", revalue)):
        res = {"all": [0, 0], "plain": [0, 0]}
        for occ, m, _text in pool:
            new = make(occ, m)
            if not new or new == occ.sentence:
                continue
            claims = ex.extract(new).claims
            if not claims:
                continue
            planted = [(Occurrence(claim_id(c.text), "psg:planted", "planted.md", "Planted", c.text), tuple(c.mentions), c.text)
                       for c in claims]
            found = detect([b for b in base if set(b[1]) & set(m)] + planted)
            hit = any(f.kind == "conflict" and any(o["path"] == "planted.md" for v in f.values for o in v["occurrences"])
                      for f in found.values())
            res["all"][0] += 1
            res["all"][1] += hit
            if plain(occ, m):
                res["plain"][0] += 1
                res["plain"][1] += hit
            if res["all"][0] >= PLANTS:
                break
        out[kind] = res
    return out


def main() -> None:
    repo = ensure_clone(HERE / ".work")
    result: dict = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "dakc_version": dakc.__version__, "source": {"repo": "fastapi/fastapi", "commit": END}}
    with tempfile.TemporaryDirectory(prefix="dakc-conflicts-") as tmp:
        ws = Path(tmp)
        docs = ws / "docs"
        docs.mkdir()
        archive = subprocess.run(["git", "-C", str(repo), "archive", END, DOCS], check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", str(docs)], input=archive, check=True)
        (ws / "dakc.toml").write_text(f'[dakc]\nsource_dir = "docs/{DOCS}"\nprovider = "mock"\njobs = 8\n', encoding="utf-8")

        spent: list[float] = []
        original = Compiler._conflicts

        def timed(self, *a, **k):
            t = time.perf_counter()
            try:
                return original(self, *a, **k)
            finally:
                spent.append(time.perf_counter() - t)

        Compiler._conflicts = timed  # type: ignore[method-assign]
        with dakc.open(ws) as kb:
            print("compiling FastAPI's docs (mock provider) ...", flush=True)
            kb.compile()
            base = occurrences(kb.store)
            found = kb.conflicts(scoped=True, accepted=True)
            rows = index_rows(base)
            by_key = defaultdict(list)
            for r in rows:
                by_key[r[0]].append(r)
            shared = {k: rs for k, rs in by_key.items() if len({r[1] for r in rs}) > 1}
            counts = kb.store.counts()
            result["precision"] = {
                "sources": counts.get("source", 0), "claims": counts.get("claim", 0), "occurrences": len(base),
                "keys": len(by_key), "keys_with_several_claims": len(shared),
                "findings": {k: sum(1 for c in found if c.kind == k) for k in ("conflict", "scoped")},
                "shared_keys": [{"key": k, "values": sorted({r[3] for r in rs}),
                                 "statements": [{"path": r[6], "sentence": r[8]} for r in rs[:5]]} for k, rs in sorted(shared.items())],
            }
            print(f"findings: {result['precision']['findings']}; {len(shared)} properties stated by several claims", flush=True)

            page = docs / DOCS / "planted-edit.md"
            page.write_text("# Planted\n\nPostgreSQL is the primary database.\n", encoding="utf-8")
            spent.clear()
            kb.compile()
            one_edit = spent[-1]
            kb.store.set_meta("conflicts_fp", "")  # as after a change to the scope rules
            spent.clear()
            kb.compile()
            result["cost_seconds"] = {"one_file_edit": round(one_edit, 5), "rejudge_everything": round(spent[-1], 3)}
            print(f"conflict step: {one_edit * 1000:.1f} ms for a one-file edit, {spent[-1]:.2f} s to re-judge everything")
        Compiler._conflicts = original  # type: ignore[method-assign]

    print("planting contradictions ...", flush=True)
    result["recall"] = recall(base)
    for kind, r in result["recall"].items():
        a, p = r["all"], r["plain"]
        print(f"{kind}: {a[1]}/{a[0]} all, {p[1]}/{p[0]} plain statements")

    out = HERE / "results" / "conflicts.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    pr, rc = result["precision"], result["recall"]

    def pct(x: list[int]) -> str:
        return f"{x[1]} / {x[0]} ({100 * x[1] / max(x[0], 1):.0f}%)"

    md = [
        "# Conflict detection on FastAPI's documentation",
        "",
        f"_Generated by `examples/fastapi-docs/conflicts_eval.py` ({result['generated']}, DAKC {result['dakc_version']}). "
        f"Corpus: FastAPI's English docs at [`{END[:8]}`](https://github.com/fastapi/fastapi/commit/{END}), "
        "compiled with the deterministic mock provider. Design: [RFC 0002](../../../docs/rfcs/0002-conflicts.md)._",
        "",
        "## Precision",
        "",
        f"{pr['sources']} documents, {pr['claims']:,} claims, {pr['occurrences']:,} claim occurrences: "
        f"**{pr['findings']['conflict']} conflicts** and {pr['findings']['scoped']} scoped differences.",
        "",
        f"The detection index has {pr['keys']:,} comparable keys. {pr['keys_with_several_claims']} of them are stated by more "
        "than one claim; these are the only places a finding could come from. (`neg·` keys compare a statement with its "
        "negation, `val·` keys a single-valued property's values.)",
        "",
        "| Key | Values | Stated in |",
        "|---|---|---|",
    ]
    for s in pr["shared_keys"]:
        md.append(f"| `{s['key'][:70].replace('|', '·')}` | {', '.join(s['values'])} | "
                  + ", ".join(sorted({x['path'] for x in s['statements']})) + " |")
    md += [
        "",
        "## Recall on planted contradictions",
        "",
        f"Up to {PLANTS} sentences per kind (seed {SEED}), each negated or given a different value for a single-valued "
        "property, extracted with the same extractor, and checked for a conflict against the original.",
        "",
        "| Planted | All sentences | Plain statements |",
        "|---|---|---|",
        f"| negation | {pct(rc['negation']['all'])} | {pct(rc['negation']['plain'])} |",
        f"| a different value | {pct(rc['changed value']['all'])} | {pct(rc['changed value']['plain'])} |",
        "",
        "Plain statements exclude list fragments, instructions (\"You …\", \"If …\") and notes.",
        "",
        "## Cost",
        "",
        f"- A one-file edit: the conflict step took {result['cost_seconds']['one_file_edit'] * 1000:.1f} ms.",
        f"- Re-judging every claim (after a change to the scope rules): {result['cost_seconds']['rejudge_everything']:.2f} s.",
        "- No model is called.",
    ]
    out.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"wrote {out} and {out.with_suffix('.md')}")


if __name__ == "__main__":
    main()
