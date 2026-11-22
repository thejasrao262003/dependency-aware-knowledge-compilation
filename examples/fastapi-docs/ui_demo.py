"""Set up the web UI demo on FastAPI's docs: two builds of history and one pending change.

  build 1   the docs before fastapi/fastapi@55ee14be added the request-body-limits page
  build 2   the docs just before fastapi/fastapi@b0a616d9 (the page exists; History shows the diff)
  pending   b0a616d9 checked out but not compiled: Changes shows the plan (20 LLM calls)

Then:  dakc serve -w examples/fastapi-docs/.work/ui-demo-kb     and open http://127.0.0.1:8765/

The mock provider produces the same knowledge as the free heuristic provider but counts
every LLM call, so the UI can show what each build would cost with a real model.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from replay import DOCS, ensure_clone, git  # noqa: E402

import dakc  # noqa: E402


def main() -> None:
    work = HERE / ".work"
    repo = ensure_clone(work)
    ws = work / "ui-demo-kb"
    shutil.rmtree(ws, ignore_errors=True)
    ws.mkdir(parents=True)
    (ws / "dakc.toml").write_text(f'[dakc]\nsource_dir = "../fastapi/{DOCS}"\nprovider = "mock"\n', encoding="utf-8")
    for ref in ("55ee14be^", "b0a616d9^"):
        git(repo, "checkout", "-q", git(repo, "rev-parse", ref).strip())
        with dakc.open(ws) as kb:
            r = kb.compile()
            print(f"build {r.build_id}: {r.status}, {r.llm_calls():,} LLM calls")
    git(repo, "checkout", "-q", git(repo, "rev-parse", "b0a616d9").strip())
    with dakc.open(ws) as kb:
        plan = kb.plan()
        print(f"pending: {plan.llm_calls()} LLM calls to catch up")
    print(f"\nnow run:  dakc serve -w {ws.relative_to(Path.cwd()) if ws.is_relative_to(Path.cwd()) else ws}")


if __name__ == "__main__":
    main()
