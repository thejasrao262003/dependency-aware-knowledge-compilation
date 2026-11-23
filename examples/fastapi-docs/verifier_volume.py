"""How much work the conflict verifier (RFC 0002, phase 2) asks of a model on FastAPI's docs.

    python examples/fastapi-docs/verifier_volume.py [--commits 50] [--price 0.25:2.00]

1. Turning the verifier on for the docs at the pinned commit: every candidate pair is judged once.
2. Keeping it on through the last N commits that changed the docs, compiled incrementally:
   only pairs that involve a new claim are judged.

The verifier is the mock provider's: it makes no network call, and counts each batch of
pairs as an LLM call with tokens estimated from the real prompt (ceil(chars / 4)). Its
verdicts are the rules' (it finds nothing new), so this measures volume and cost, not quality;
``benchmarks/conflicts/evaluate.py`` measures quality. Claims come from the heuristic
extractor: an LLM extractor yields different claims, so treat the numbers as estimates.

``--price INPUT:OUTPUT`` is USD per million tokens, an assumption you set (default: a small
model's price). The mock's token estimates assume short answers. A reasoning model writes
far more: ``--tokens-per-pair INPUT:OUTPUT`` prices the same pairs at rates measured with a
real model instead. The default, 101:124, is gpt-5-mini's on the labelled pairs
(``benchmarks/conflicts/results-verifier-gpt-5-mini-before-fixes.json``: 14 calls, 127
pairs, 12,891 input and 15,786 output tokens; a second run measured 98:121).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from replay import DOCS, END, ensure_clone, git  # noqa: E402

import dakc  # noqa: E402
from dakc.compiler import Compiler  # noqa: E402
from dakc.conflicts.verifier import Settings  # noqa: E402
from dakc.providers import make_provider  # noqa: E402
from dakc.storage import Store  # noqa: E402

SETTINGS = Settings(max_checks=10**9)  # unbounded, to see the whole volume


class Counting:
    """Wraps a verifier and adds up what it is asked."""

    def __init__(self, inner):
        self.inner, self.fingerprint, self.is_llm, self.free = inner, inner.fingerprint, True, False
        self.calls = self.pairs = self.input_tokens = self.output_tokens = 0

    def judge(self, pairs):
        res = self.inner.judge(pairs)
        self.calls += 1
        self.pairs += len(pairs)
        self.input_tokens += res.input_tokens
        self.output_tokens += res.output_tokens
        return res

    def snapshot(self) -> tuple[int, int, int, int]:
        return self.calls, self.pairs, self.input_tokens, self.output_tokens


def compile_with(store: Store, src: Path, verifier: Counting):
    bundle = make_provider("mock")
    bundle.verifier = verifier
    t = time.perf_counter()
    rep, _ = Compiler(store, bundle, src, jobs=8, acquire_lock=False, conflict_verifier=SETTINGS).run()
    return rep, time.perf_counter() - t


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commits", type=int, default=50)
    ap.add_argument("--price", default="0.25:2.00", help="USD per million input:output tokens")
    ap.add_argument("--tokens-per-pair", default="101:124", help="measured input:output tokens per judged pair")
    args = ap.parse_args()
    p_in, p_out = (float(x) for x in args.price.split(":"))
    r_in, r_out = (float(x) for x in args.tokens_per_pair.split(":"))
    usd = lambda i, o: i / 1e6 * p_in + o / 1e6 * p_out  # noqa: E731
    measured = lambda pairs: usd(pairs * r_in, pairs * r_out)  # noqa: E731

    repo = ensure_clone(HERE / ".work")
    commits = git(repo, "rev-list", "--first-parent", "-n", str(args.commits), END, "--", DOCS).split()[::-1]
    base = git(repo, "rev-parse", f"{commits[0]}^").strip()
    out: dict = {"generated": datetime.now(UTC).isoformat(timespec="seconds"), "dakc_version": dakc.__version__,
                 "source": {"repo": "fastapi/fastapi", "commit": END, "base": base}, "settings": SETTINGS.__dict__,
                 "price_per_million": {"input": p_in, "output": p_out},
                 "measured_tokens_per_pair": {"input": r_in, "output": r_out}}
    with tempfile.TemporaryDirectory(prefix="dakc-verifier-") as tmp:
        tmp = Path(tmp)
        src = tmp / "docs"

        def checkout(sha: str) -> None:
            shutil.rmtree(src, ignore_errors=True)
            src.mkdir()
            arch = subprocess.run(["git", "-C", str(repo), "archive", sha, DOCS], check=True, capture_output=True).stdout
            subprocess.run(["tar", "-x", "-C", str(src)], input=arch, check=True)

        # 1. turning it on at the pinned commit
        checkout(END)
        v = Counting(make_provider("mock").verifier)
        store = Store(tmp / "end.db")
        rep, secs = compile_with(store, src / DOCS, v)
        calls, pairs, ti, to = v.snapshot()
        out["turn_on"] = {"claims": rep.counts.get("claims_total"), "candidate_pairs": rep.counts.get("conflict_candidates"),
                          "pairs_judged": pairs, "calls": calls, "input_tokens": ti, "output_tokens": to,
                          "usd": round(usd(ti, to), 4), "seconds": round(secs, 1)}
        print("turn on:", out["turn_on"], flush=True)
        store.close()

        # 2. the replay, verifier on from the base commit
        checkout(base)
        v = Counting(make_provider("mock").verifier)
        store = Store(tmp / "replay.db")
        compile_with(store, src / DOCS, v)
        rows = []
        for i, sha in enumerate(commits, 1):
            checkout(sha)
            before = v.snapshot()
            rep, secs = compile_with(store, src / DOCS, v)
            after = v.snapshot()
            d = [a - b for a, b in zip(after, before)]
            rows.append({"n": i, "commit": sha[:10], "pairs_judged": d[1], "calls": d[0], "input_tokens": d[2],
                         "output_tokens": d[3], "seconds": round(secs, 2)})
            print(rows[-1], flush=True)
        store.close()
    tot = {k: sum(r[k] for r in rows) for k in ("pairs_judged", "calls", "input_tokens", "output_tokens")}
    tot["usd"] = round(usd(tot["input_tokens"], tot["output_tokens"]), 4)
    tot["commits_with_no_calls"] = sum(1 for r in rows if r["calls"] == 0)
    tot["max_pairs_in_one_commit"] = max(r["pairs_judged"] for r in rows)
    out["replay"] = {"commits": len(rows), "totals": tot, "per_commit": rows}
    res = HERE / "results" / "verifier-volume.json"
    res.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")

    t, on = tot, out["turn_on"]
    md = [
        "# Conflict verifier: volume on FastAPI's documentation",
        "",
        f"_Generated by `examples/fastapi-docs/verifier_volume.py` ({out['generated']}, DAKC {out['dakc_version']}). "
        f"FastAPI's English docs at [`{END[:8]}`](https://github.com/fastapi/fastapi/commit/{END}); claims from the heuristic "
        f"extractor; tokens estimated from the verifier's real prompt; dollars at ${p_in:.2f} / ${p_out:.2f} per million "
        "input / output tokens (an assumption: pass `--price`)._",
        "",
        "| | Pairs judged | Model calls | Input / output tokens (estimated) | Cost at those | "
        f"Cost at {r_in:.0f} / {r_out:.0f} tokens per pair (measured) |",
        "|---|---:|---:|---:|---:|---:|",
        f"| Turning the verifier on ({on['claims']:,} claims) | {on['pairs_judged']:,} | {on['calls']:,} | "
        f"{on['input_tokens']:,} / {on['output_tokens']:,} | ${on['usd']:.2f} | ${measured(on['pairs_judged']):.2f} |",
        f"| Keeping it on through {len(rows)} commits | {t['pairs_judged']:,} | {t['calls']:,} | {t['input_tokens']:,} / "
        f"{t['output_tokens']:,} | ${t['usd']:.3f} | ${measured(t['pairs_judged']):.3f} |",
        "",
        f"{t['commits_with_no_calls']} of the {len(rows)} commits needed no verifier call; the busiest judged "
        f"{t['max_pairs_in_one_commit']} pairs. A verdict is cached by the pair of claims, so a pair is never judged twice.",
    ]
    res.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"wrote {res} and {res.with_suffix('.md')}")


if __name__ == "__main__":
    main()
