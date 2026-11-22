"""Replay real documentation history through DAKC.

Takes the last N commits that changed FastAPI's English documentation
(``docs/en/docs``, MIT licensed) up to a pinned commit, and replays them in
order. At every commit it measures what keeping a knowledge base of those
docs current costs under three strategies, all with DAKC's deterministic mock
provider (LLM calls counted exactly, tokens estimated as ceil(chars/4)):

  full   rebuild everything from scratch. Also the correctness oracle.
  file   re-process every changed file: what file-level LLM-wiki tools do.
  dakc   incremental and claim-level; the knowledge base is carried from
         commit to commit, exactly as a CI job or ``mkdocs serve`` would.

It also checks that DAKC's incrementally maintained state is identical to
the full rebuild at every single commit.

FastAPI's docs are not copied into this repository: the script clones them
(blobs on demand, docs only) at the pinned commits.

Usage:
  python examples/fastapi-docs/replay.py                  # 50 commits up to the pinned END
  python examples/fastapi-docs/replay.py --commits 10     # quicker
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from dakc import __version__
from dakc.compiler import Compiler
from dakc.provenance.compare import canonical_state, diff_states
from dakc.providers import make_provider
from dakc.storage import Store

HERE = Path(__file__).resolve().parent
REPO_URL = "https://github.com/fastapi/fastapi.git"
END = "05e6df7e906c3decba377c37548019973f40bf15"  # 0.165.0, 2026-11-18 (pinned for reproducibility)
DOCS = "docs/en/docs"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def ensure_clone(work: Path) -> Path:
    repo = work / "fastapi"
    if not (repo / ".git").exists():
        work.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--no-checkout", REPO_URL, str(repo)], check=True)
        git(repo, "sparse-checkout", "set", DOCS)
    try:
        git(repo, "cat-file", "-e", END)
    except subprocess.CalledProcessError:
        git(repo, "fetch", "-q", "origin")
    return repo


def compile_once(db: Path, src: Path, strategy: str) -> tuple[dict, Store]:
    store = Store(db)
    t0 = time.perf_counter()
    rep, _ = Compiler(store, make_provider("mock"), src, strategy=strategy, jobs=1, acquire_lock=False).run()
    return {
        "seconds": round(time.perf_counter() - t0, 3),
        "status": rep.status,
        "llm_calls": rep.usage.llm_calls,
        "input_tokens_est": rep.usage.input_tokens,
        "output_tokens_est": rep.usage.output_tokens,
        "passages_reextracted": rep.usage.by_stage.get("extract", 0),
        "pages_regenerated": rep.n("regenerate_page"),
        "pages_cutoff": rep.n("cutoff_page"),
        "summaries_regenerated": rep.n("regenerate_summary"),
    }, store


def close(store: Store) -> None:
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commits", type=int, default=50)
    ap.add_argument("--end", default=END)
    ap.add_argument("--work", type=Path, default=HERE / ".work")
    ap.add_argument("--out", type=Path, default=HERE / "results" / "replay.json")
    args = ap.parse_args()

    repo = ensure_clone(args.work)
    commits = git(repo, "rev-list", "--first-parent", "-n", str(args.commits), args.end, "--", DOCS).split()[::-1]
    base = git(repo, "rev-parse", f"{commits[0]}^").strip()
    src = repo / DOCS
    state = args.work / "state"
    shutil.rmtree(state, ignore_errors=True)
    state.mkdir(parents=True)
    main_db = state / "dakc.db"

    git(repo, "checkout", "-q", base)
    initial, store = compile_once(main_db, src, "dakc")
    counts = store.counts()
    close(store)
    print(f"base {base[:8]}: initial build {initial['seconds']}s, {initial['llm_calls']} LLM calls, "
          f"{counts['source']} documents, {counts['passage']} passages, {counts['claim']} claims, {counts['page']} pages", flush=True)

    rows, prev = [], base
    for i, sha in enumerate(commits, 1):
        subject = git(repo, "log", "-1", "--format=%s", sha).strip()
        date = git(repo, "log", "-1", "--format=%cs", sha).strip()
        numstat = [line.split("\t") for line in git(repo, "diff", "--numstat", prev, sha, "--", DOCS).splitlines()]
        files = [f[2].removeprefix(DOCS + "/") for f in numstat]
        added = sum(int(f[0]) for f in numstat if f[0].isdigit())
        removed = sum(int(f[1]) for f in numstat if f[1].isdigit())
        git(repo, "checkout", "-q", sha)

        file_db = state / "file.db"
        shutil.copy(main_db, file_db)
        file_m, s = compile_once(file_db, src, "file")
        close(s)
        file_db.unlink()

        dakc_m, dakc_store = compile_once(main_db, src, "dakc")
        full_db = state / "full.db"
        full_db.unlink(missing_ok=True)
        full_m, full_store = compile_once(full_db, src, "full")
        diff = diff_states(canonical_state(full_store), canonical_state(dakc_store))
        close(full_store)
        close(dakc_store)
        full_db.unlink()

        row = {
            "n": i, "commit": sha[:10], "date": date, "subject": subject[:100],
            "files": files, "lines_added": added, "lines_removed": removed,
            "full": full_m, "file": file_m, "dakc": dakc_m,
            "equal_to_full_rebuild": diff.equal, "differing_artifacts": len(diff.artifact_keys()),
        }
        rows.append(row)
        prev = sha
        print(f"[{i:2d}/{len(commits)}] {sha[:8]} {date} +{added}/-{removed} in {len(files)} file(s) | LLM calls "
              f"full {full_m['llm_calls']:,} / file {file_m['llm_calls']:,} / dakc {dakc_m['llm_calls']:,} | "
              f"pages file {file_m['pages_regenerated']} / dakc {dakc_m['pages_regenerated']} | equal {diff.equal}", flush=True)

    totals = {s: {k: sum(r[s][k] for r in rows) for k in ("llm_calls", "input_tokens_est", "output_tokens_est",
                                                         "passages_reextracted", "pages_regenerated", "seconds")}
              for s in ("full", "file", "dakc")}
    out = {
        "generated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dakc_version": __version__,
        "source": {"repository": REPO_URL, "path": DOCS, "license": "MIT", "base": base, "end": args.end},
        "provider": "mock (deterministic; LLM calls counted exactly, tokens estimated as ceil(chars/4))",
        "initial_build": {**initial, "counts": counts},
        "commits": rows,
        "totals": totals,
        "all_equal_to_full_rebuild": all(r["equal_to_full_rebuild"] for r in rows),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=1), encoding="utf-8")
    t = totals
    print(f"\n{len(rows)} commits: LLM calls full {t['full']['llm_calls']:,} / file {t['file']['llm_calls']:,} / "
          f"dakc {t['dakc']['llm_calls']:,}; every commit equal to a full rebuild: {out['all_equal_to_full_rebuild']}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
