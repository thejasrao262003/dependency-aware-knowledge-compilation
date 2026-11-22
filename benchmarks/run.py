"""Benchmark: full rebuild vs file-level invalidation vs DAKC.

For each corpus size and change scenario (docs/benchmark.md):

  A  = full build of the original corpus (fresh store)
  B  = full build of the mutated corpus (fresh store) -- the oracle
  Δ  = artifacts whose canonical state differs between A and B
  I_s = copy of A's store, mutated corpus, compiled with strategy s in {full, file, dakc}

and every metric is measured, not estimated, except token counts, which the
deterministic mock provider estimates as ceil(chars/4) (labelled as such).

Usage:  python -m benchmarks.run --sizes 100,1000,5000 --reps 3
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.corpus import SCENARIOS, apply_scenario, generate
from dakc.compiler import Compiler
from dakc.provenance.compare import canonical_ids, canonical_state, diff_states
from dakc.providers import make_provider
from dakc.storage import Store

STRATEGIES = ("full", "file", "dakc")
ROOT = Path(__file__).resolve().parents[1]


def _close(store: Store) -> None:
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store.close()


def _compile(db: Path, corpus: Path, strategy: str):
    store = Store(db)
    t0 = time.perf_counter()
    report, _ = Compiler(store, make_provider("mock"), corpus, strategy=strategy, jobs=1).run()
    elapsed = time.perf_counter() - t0
    return store, report, elapsed


def run_size(n_docs: int, reps: int, work: Path, log, scenarios: list[str] | None = None, profile: str = "uniform") -> dict:
    wd = work / f"n{n_docs}"
    shutil.rmtree(wd, ignore_errors=True)
    wd.mkdir(parents=True)
    generate(wd / "corpus", n_docs, profile=profile)
    manifest = json.loads((wd / "corpus.manifest.json").read_text())

    store_a, rep_a, t_a = _compile(wd / "A.db", wd / "corpus", "full")
    canon_a = canonical_state(store_a)
    ids_a = canonical_ids(store_a)
    counts_a = store_a.counts()
    _close(store_a)
    log(f"[{n_docs}] initial full build: {t_a:.2f}s, {rep_a.usage.llm_calls} LLM calls, {counts_a}")

    out = {"n_docs": n_docs, "profile": profile, "initial_build": {"seconds": round(t_a, 3), "llm_calls": rep_a.usage.llm_calls, "counts": counts_a}, "scenarios": {}}
    for scenario in scenarios or list(SCENARIOS):
        corpus = wd / f"corpus-{scenario}"
        shutil.copytree(wd / "corpus", corpus)
        change = apply_scenario(corpus, manifest, scenario)
        store_b, rep_b, t_b = _compile(wd / "B.db", corpus, "full")
        canon_b = canonical_state(store_b)
        _close(store_b)
        (wd / "B.db").unlink()
        delta = diff_states(canon_a, canon_b).artifact_keys()
        sc = {"change": change, "oracle_changed_artifacts": len(delta), "strategies": {}}

        for strategy in STRATEGIES:
            times, last = [], None
            for _ in range(reps):
                db = wd / "I.db"
                shutil.copy(wd / "A.db", db)
                store_i, rep, elapsed = _compile(db, corpus, strategy)
                times.append(elapsed)
                if last is not None:
                    _close(store_i)
                    continue
                bid = rep.build_id
                ids_i = canonical_ids(store_i)
                written = {r[0] for r in store_i.conn.execute("SELECT id FROM artifacts WHERE from_build=?", (bid,))}
                deleted = {r[0] for r in store_i.conn.execute("SELECT id FROM artifacts WHERE to_build=?", (bid,))} - written
                modified_keys = {ids_i[i] for i in written if i in ids_i} | {ids_a.get(i, i) for i in deleted}
                regenerated = {f"page:{p}" for p in rep.ops.get("regenerate_page", [])} | {f"summary:{s}" for s in rep.ops.get("regenerate_summary", [])}
                d = diff_states(canon_b, canonical_state(store_i))
                stale_keys = d.artifact_keys()
                last = {
                    "processed_sources": rep.counts.get("sources_processed", 0),
                    "passages_reextracted": rep.usage.by_stage.get("extract", 0),
                    "claims_regenerated": rep.counts.get("claims_from_provider", 0),
                    "pages_regenerated": rep.n("regenerate_page"),
                    "pages_rerendered_no_llm": rep.n("rerender_page"),
                    "pages_cutoff": rep.n("cutoff_page"),
                    "summaries_regenerated": rep.n("regenerate_summary"),
                    "llm_calls": rep.usage.llm_calls,
                    "input_tokens_est": rep.usage.input_tokens,
                    "output_tokens_est": rep.usage.output_tokens,
                    "extraction_cache_hits": rep.usage.cache_hits,
                    "artifact_versions_written": len(written),
                    "unaffected_artifacts_modified": len(modified_keys - delta),
                    "unnecessary_regenerations": len(regenerated - delta),
                    "stale_artifacts_retained": len(stale_keys),
                    "edge_mismatches": len(d.missing) + len(d.extra) + len(d.different) - len(stale_keys),
                    "equivalent_to_full_rebuild": d.equal,
                    "status": rep.status,
                }
                _close(store_i)
            last["seconds_median"] = round(statistics.median(times), 4)
            last["seconds_all"] = [round(t, 4) for t in times]
            sc["strategies"][strategy] = last
            (wd / "I.db").unlink(missing_ok=True)
        out["scenarios"][scenario] = sc
        s = sc["strategies"]
        log(
            f"[{n_docs}] {scenario}: LLM full/file/dakc = {s['full']['llm_calls']}/{s['file']['llm_calls']}/{s['dakc']['llm_calls']}, "
            f"pages {s['full']['pages_regenerated']}/{s['file']['pages_regenerated']}/{s['dakc']['pages_regenerated']}, "
            f"stale {s['file']['stale_artifacts_retained']}/{s['dakc']['stale_artifacts_retained']}, "
            f"eq {s['full']['equivalent_to_full_rebuild']}/{s['file']['equivalent_to_full_rebuild']}/{s['dakc']['equivalent_to_full_rebuild']}"
        )
        shutil.rmtree(corpus)
    return out


def machine() -> dict:
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip() or "uncommitted"
    except OSError:
        commit = "unknown"
    try:
        cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
    except OSError:
        cpu = ""
    return {
        "date": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platform": platform.platform(),
        "cpu": cpu or platform.processor(),
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
        "git_commit": commit,
        "provider": "mock (deterministic; tokens estimated as ceil(chars/4))",
    }


TIMING_KEYS = ("seconds_median", "seconds_all", "seconds")


def strip_timing(obj):
    if isinstance(obj, dict):
        return {k: strip_timing(v) for k, v in obj.items() if k not in TIMING_KEYS}
    if isinstance(obj, list):
        return [strip_timing(x) for x in obj]
    return obj


def verify_against(results: dict, reference: dict) -> list[str]:
    """Every non-timing metric of every (size, scenario, strategy) must reproduce exactly."""
    ref_sizes = {s["n_docs"]: s for s in reference["sizes"]}
    problems = []
    for size in results["sizes"]:
        ref = ref_sizes.get(size["n_docs"])
        if ref is None:
            problems.append(f"size {size['n_docs']} not in reference")
            continue
        for key, sc in size["scenarios"].items():
            if key not in ref["scenarios"]:
                problems.append(f"{size['n_docs']}/{key}: not in reference")
                continue
            a, b = strip_timing(sc), strip_timing(ref["scenarios"][key])
            if a != b:
                for strat in a.get("strategies", {}):
                    for metric, v in a["strategies"][strat].items():
                        rv = b.get("strategies", {}).get(strat, {}).get(metric)
                        if rv != v:
                            problems.append(f"{size['n_docs']}/{key}/{strat}/{metric}: {v} != reference {rv}")
                if a.get("change") != b.get("change") or a.get("oracle_changed_artifacts") != b.get("oracle_changed_artifacts"):
                    problems.append(f"{size['n_docs']}/{key}: corpus mutation or oracle differs")
    return problems


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="100,1000,5000")
    ap.add_argument("--reps", type=int, default=3, help="timing repetitions (median reported; sizes > 1000 use 1)")
    ap.add_argument("--scenarios", default="", help="comma-separated subset (default: all)")
    ap.add_argument("--profile", default="uniform", choices=["uniform", "realistic"])
    ap.add_argument("--work", type=Path, default=ROOT / "bench-work")
    ap.add_argument("--out", type=Path, default=ROOT / "benchmarks" / "results" / "latest.json")
    ap.add_argument("--verify-against", type=Path, help="fail unless every non-timing metric matches this results file")
    args = ap.parse_args()
    scenarios = [s for s in args.scenarios.split(",") if s] or None
    results = {"machine": machine(), "reps": args.reps, "profile": args.profile, "sizes": []}
    for n in (int(s) for s in args.sizes.split(",")):
        reps = args.reps if n <= 1000 else 1
        results["sizes"].append(run_size(n, reps, args.work, lambda m: print(m, flush=True), scenarios, args.profile))
        results["sizes"][-1]["reps"] = reps
    if args.verify_against:
        problems = verify_against(results, json.loads(args.verify_against.read_text(encoding="utf-8")))
        for p in problems[:50]:
            print("MISMATCH", p)
        print("benchmark reproduced exactly (all non-timing metrics)" if not problems else f"{len(problems)} mismatch(es)")
        raise SystemExit(1 if problems else 0)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, indent=1, sort_keys=True), encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
