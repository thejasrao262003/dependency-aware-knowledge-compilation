"""What keeping FastAPI's documentation knowledge current costs with a real model, in dollars.

replay.py counts LLM calls exactly but estimates tokens. This script measures them:

1. It builds the knowledge base at the replay's base commit with a real LLM provider.
2. It compiles the same commits incrementally with DAKC.
3. It logs the real input and output tokens of every provider call, by stage
   (extract, page, summary).

Failed attempts are logged too, so the spend reported is the spend incurred.

The other two strategies are not run for real: re-processing changed files and
rebuilding everything would cost hundreds to thousands of times more. They are
priced from exact per-stage call counts (the deterministic mock provider, as in
replay.py) multiplied by the real tokens per call measured here for each stage.
Their rows are estimates; DAKC's row is measured.

Usage:
  python examples/fastapi-docs/cost.py --provider-config provider.toml \\
      --price "gpt-5-mini:0.25:2.00" [--price "gpt-5.4-mini:0.75:4.50"] \\
      [--per-call-overhead 0] [--commits 50] [--jobs 16]

  --price LABEL:INPUT:OUTPUT   USD per million tokens. The first is the model you
                               measured with; others reuse its token counts, so they
                               are approximate (tokenizers and output lengths differ).
  --per-call-overhead N        input tokens a gateway or proxy adds to every call,
                               subtracted so results reflect calling the model API directly.

The run resumes after an interruption: progress is kept under --work.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import time
from datetime import UTC, date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE), str(ROOT)]

from replay import DOCS, END, REPO_URL, ensure_clone, git  # noqa: E402

from benchmarks.llm_experiment import load_bundle  # noqa: E402
from dakc import __version__  # noqa: E402
from dakc.compiler import Compiler  # noqa: E402
from dakc.providers import make_provider  # noqa: E402
from dakc.provenance.verify import verify_store  # noqa: E402
from dakc.storage import Store  # noqa: E402

STAGES = ("extract", "page", "summary")


class Meter:
    """Counts real provider calls and tokens per stage, including attempts whose build later failed."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls = dict.fromkeys(STAGES, 0)
        self.inp = dict.fromkeys(STAGES, 0)
        self.out = dict.fromkeys(STAGES, 0)

    def add(self, stage: str, calls: int, inp: int, out: int) -> None:
        with self.lock:
            self.calls[stage] += calls
            self.inp[stage] += inp
            self.out[stage] += out

    def snapshot(self) -> dict:
        with self.lock:
            return {"calls": dict(self.calls), "input": dict(self.inp), "output": dict(self.out)}


def delta(after: dict, before: dict) -> dict:
    return {k: {s: after[k][s] - before[k][s] for s in STAGES} for k in after}


class _Metered:
    def __init__(self, inner, stage: str, meter: Meter):
        self.inner, self.stage, self.meter = inner, stage, meter
        self.fingerprint, self.is_llm, self.free = inner.fingerprint, inner.is_llm, inner.free

    def extract(self, text, targets=None):
        r = self.inner.extract(text, targets)
        self.meter.add("extract", getattr(r, "calls", 1), r.input_tokens, r.output_tokens)
        return r

    def write(self, name, claims):
        r = self.inner.write(name, claims)
        self.meter.add("page", 1, r.input_tokens, r.output_tokens)
        return r

    def summarize(self, topic, pages):
        r = self.inner.summarize(topic, pages)
        self.meter.add("summary", 1, r.input_tokens, r.output_tokens)
        return r


def metered(bundle, meter: Meter):
    bundle.extractor = _Metered(bundle.extractor, "extract", meter)
    bundle.writer = _Metered(bundle.writer, "page", meter)
    bundle.summarizer = _Metered(bundle.summarizer, "summary", meter)
    return bundle


def compile_real(store: Store, bundle, src: Path, jobs: int, meter: Meter, attempts: int = 6) -> tuple[dict, dict]:
    """Compile with retries. Extraction results are cached by content, so a retry does not re-pay them."""
    before = meter.snapshot()
    for attempt in range(attempts):
        try:
            rep, _ = Compiler(store, bundle, src, jobs=jobs, acquire_lock=False).run()
            break
        except Exception as exc:  # network, rate limits: wait and resume from the cache
            if attempt == attempts - 1:
                raise
            print(f"  compile failed ({type(exc).__name__}: {str(exc)[:120]}); retrying in 60 s", flush=True)
            time.sleep(60)
    spent = delta(meter.snapshot(), before)
    ops = {"status": rep.status, "passages_reextracted": rep.usage.by_stage.get("extract", 0),
           "pages_regenerated": rep.n("regenerate_page"), "sections_regenerated": rep.n("regenerate_section"),
           "summaries_regenerated": rep.n("regenerate_summary"), "llm_calls_in_build": rep.usage.llm_calls}
    return spent, ops


def mock_counts(db: Path, src: Path, strategy: str) -> dict[str, int]:
    store = Store(db)
    rep, _ = Compiler(store, make_provider("mock"), src, strategy=strategy, jobs=1, acquire_lock=False).run()
    store.close()
    return {s: rep.usage.by_stage.get(s, 0) + rep.usage.by_stage.get(f"{s}_retry", 0) for s in STAGES}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider-config", type=Path, required=True)
    ap.add_argument("--price", action="append", required=True, help="LABEL:INPUT:OUTPUT in USD per million tokens")
    ap.add_argument("--per-call-overhead", type=int, default=0)
    ap.add_argument("--commits", type=int, default=50)
    ap.add_argument("--end", default=END)
    ap.add_argument("--jobs", type=int, default=16)
    ap.add_argument("--work", type=Path, default=HERE / ".work")
    ap.add_argument("--out", type=Path, default=HERE / "results" / "cost.json")
    args = ap.parse_args()
    prices = []
    for p in args.price:
        label, pin, pout = p.rsplit(":", 2)
        prices.append({"label": label, "input": float(pin), "output": float(pout)})

    repo = ensure_clone(args.work)
    commits = git(repo, "rev-list", "--first-parent", "-n", str(args.commits), args.end, "--", DOCS).split()[::-1]
    base = git(repo, "rev-parse", f"{commits[0]}^").strip()
    src = repo / DOCS
    state = args.work / "cost-state"
    state.mkdir(parents=True, exist_ok=True)
    progress_path = state / "progress.json"
    progress = json.loads(progress_path.read_text()) if progress_path.exists() else {"base": None, "commits": []}

    meter = Meter()
    bundle = metered(load_bundle(args.provider_config), meter)
    real_db, mock_db = state / "real.db", state / "mock.db"

    def save() -> None:
        progress_path.write_text(json.dumps(progress, indent=1), encoding="utf-8")

    if progress["base"] is None:
        # Restarting before the base build committed: the deterministic store is rebuilt, but the
        # real one is kept, because its extraction cache holds every call already paid for.
        for suffix in ("", "-wal", "-shm"):
            Path(str(mock_db) + suffix).unlink(missing_ok=True)
        git(repo, "checkout", "-q", base)
        mock_counts(mock_db, src, "dakc")
        t0 = time.perf_counter()
        store = Store(real_db)
        spent, ops = compile_real(store, bundle, src, args.jobs, meter)
        counts = store.counts()
        store.close()
        progress["base"] = {"commit": base, "seconds": round(time.perf_counter() - t0), "spent": spent, "ops": ops,
                            "counts": counts, "mock_full": mock_counts(state / "full.db", src, "full")}
        (state / "full.db").unlink(missing_ok=True)
        save()
        print(f"base {base[:8]}: {sum(spent['calls'].values()):,} real calls in {progress['base']['seconds']} s", flush=True)

    done = {c["commit"] for c in progress["commits"]}
    prev = progress["commits"][-1]["sha"] if progress["commits"] else base
    for i, sha in enumerate(commits, 1):
        if sha[:10] in done:
            continue
        git(repo, "checkout", "-q", sha)
        numstat = [line.split("\t") for line in git(repo, "diff", "--numstat", prev, sha, "--", DOCS).splitlines()]
        files = [f[2].removeprefix(DOCS + "/") for f in numstat]
        pending = progress.get("pending") or {}
        if pending.get("sha") == sha:  # resumed: the deterministic state already includes this commit
            mock_dakc, mock_file, mock_full = pending["dakc"], pending["file"], pending["full"]
        else:
            file_db = state / "file.db"
            shutil.copy(mock_db, file_db)
            mock_file = mock_counts(file_db, src, "file")
            for suffix in ("", "-wal", "-shm"):
                Path(str(file_db) + suffix).unlink(missing_ok=True)
            mock_dakc = mock_counts(mock_db, src, "dakc")
            mock_full = mock_counts(state / "full.db", src, "full")
            (state / "full.db").unlink(missing_ok=True)
            progress["pending"] = {"sha": sha, "dakc": mock_dakc, "file": mock_file, "full": mock_full}
            save()
        store = Store(real_db)
        spent, ops = compile_real(store, bundle, src, args.jobs, meter)
        store.close()
        progress["commits"].append({
            "n": i, "commit": sha[:10], "sha": sha, "date": git(repo, "log", "-1", "--format=%cs", sha).strip(),
            "subject": git(repo, "log", "-1", "--format=%s", sha).strip()[:100], "files": files,
            "real_dakc": {"spent": spent, "ops": ops},
            "mock_calls": {"dakc": mock_dakc, "file": mock_file, "full": mock_full},
        })
        progress["pending"] = None
        save()
        prev = sha
        print(f"[{i:2d}/{len(commits)}] {sha[:8]} real DAKC calls {sum(spent['calls'].values())} "
              f"(mock {sum(mock_dakc.values())}), file-level {sum(mock_file.values()):,}", flush=True)

    store = Store(real_db)
    invariants_ok = verify_store(store).ok
    store.close()

    # ------------------------------------------------------------- pricing
    ov = args.per_call_overhead
    rows = progress["commits"]
    base_spent = progress["base"]["spent"]
    all_calls = {s: base_spent["calls"][s] + sum(r["real_dakc"]["spent"]["calls"][s] for r in rows) for s in STAGES}
    all_in = {s: base_spent["input"][s] + sum(r["real_dakc"]["spent"]["input"][s] for r in rows) for s in STAGES}
    all_out = {s: base_spent["output"][s] + sum(r["real_dakc"]["spent"]["output"][s] for r in rows) for s in STAGES}
    per_call = {s: {"input": (all_in[s] - ov * all_calls[s]) / all_calls[s], "output": all_out[s] / all_calls[s]}
                for s in STAGES if all_calls[s]}

    def measured_tokens(spent: dict) -> tuple[int, int]:
        calls = sum(spent["calls"].values())
        return sum(spent["input"].values()) - ov * calls, sum(spent["output"].values())

    def estimated_tokens(calls: dict) -> tuple[float, float]:
        return (sum(calls[s] * per_call[s]["input"] for s in STAGES if s in per_call),
                sum(calls[s] * per_call[s]["output"] for s in STAGES if s in per_call))

    def usd(tokens: tuple[float, float], price: dict) -> float:
        return (tokens[0] * price["input"] + tokens[1] * price["output"]) / 1e6

    dakc_tok = [measured_tokens(r["real_dakc"]["spent"]) for r in rows]
    file_tok = [estimated_tokens(r["mock_calls"]["file"]) for r in rows]
    full_tok = [estimated_tokens(r["mock_calls"]["full"]) for r in rows]
    base_tok = measured_tokens(base_spent)
    first, last = date.fromisoformat(rows[0]["date"]), date.fromisoformat(rows[-1]["date"])
    days = max((last - first).days, 1)
    per_month = len(rows) / days * 30.4

    def total(toks):
        return (sum(t[0] for t in toks), sum(t[1] for t in toks))

    priced = []
    for price in prices:
        d, f, fu = usd(total(dakc_tok), price), usd(total(file_tok), price), usd(total(full_tok), price)
        priced.append({**price, "initial_build_usd": round(usd(base_tok, price), 2),
                       "commits_usd": {"dakc": round(d, 2), "file": round(f, 2), "full": round(fu, 2)},
                       "per_month_usd": {k: round(v / len(rows) * per_month, 2) for k, v in (("dakc", d), ("file", f), ("full", fu))}})

    out = {
        "generated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dakc_version": __version__,
        "source": {"repository": REPO_URL, "path": DOCS, "license": "MIT", "base": base, "end": args.end},
        "provider": bundle.extractor.inner.fingerprint,
        "per_call_overhead_subtracted": ov,
        "jobs": args.jobs,
        "window": {"commits": len(rows), "first": rows[0]["date"], "last": rows[-1]["date"], "days": days,
                   "commits_per_month": round(per_month, 1)},
        "initial_build": {**progress["base"], "tokens": {"input": base_tok[0], "output": base_tok[1]}},
        "real_tokens_per_call": {s: {k: round(v, 1) for k, v in per_call[s].items()} for s in per_call},
        "totals": {
            "dakc_measured": {"calls": sum(sum(r["real_dakc"]["spent"]["calls"].values()) for r in rows),
                              "input_tokens": total(dakc_tok)[0], "output_tokens": total(dakc_tok)[1]},
            "file_estimated": {"calls": sum(sum(r["mock_calls"]["file"].values()) for r in rows),
                               "input_tokens": round(total(file_tok)[0]), "output_tokens": round(total(file_tok)[1])},
            "full_estimated": {"calls": sum(sum(r["mock_calls"]["full"].values()) for r in rows),
                               "input_tokens": round(total(full_tok)[0]), "output_tokens": round(total(full_tok)[1])},
            "dakc_mock_calls": sum(sum(r["mock_calls"]["dakc"].values()) for r in rows),
        },
        "prices": priced,
        "invariants_ok": invariants_ok,
        "commits": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=1), encoding="utf-8")
    p = priced[0]
    print(f"\n{len(rows)} commits over {days} days with {p['label']}: DAKC ${p['commits_usd']['dakc']} (measured), "
          f"file-level ${p['commits_usd']['file']}, full rebuilds ${p['commits_usd']['full']} (estimated); "
          f"initial build ${p['initial_build_usd']}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
