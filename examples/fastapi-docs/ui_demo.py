"""Set up the web UI demo on FastAPI's docs: two builds of history, one pending change and a conflict.

  build 1   the docs before fastapi/fastapi@55ee14be added the request-body-limits page
  build 2   the docs just before fastapi/fastapi@b0a616d9 (the page exists; History shows the diff),
            plus a team's notes copied from FastAPI's docs before 0.118.0, which still say "Bcrypt":
            Conflicts shows it against the tutorial's "Argon2" (see conflicts_walkthrough.py)
  pending   b0a616d9 checked out but not compiled: Changes shows the plan (25 LLM calls)

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

NOTES = """# Auth notes

Our services follow the FastAPI security tutorial for login.

## Password hashing

PassLib is a great Python package to handle password hashes.

The recommended algorithm is "Bcrypt".
"""


def main() -> None:
    work = HERE / ".work"
    repo = ensure_clone(work)
    ws = work / "ui-demo-kb"
    shutil.rmtree(ws, ignore_errors=True)
    ws.mkdir(parents=True)
    (ws / "dakc.toml").write_text(
        '[dakc]\nprovider = "mock"\n\n'
        f'[[sources]]\nname = "fastapi"\ntype = "filesystem"\npath = "../fastapi/{DOCS}"\nprefix = ""\n\n'
        '[[sources]]\nname = "team"\ntype = "filesystem"\npath = "team-notes"\n', encoding="utf-8")
    (ws / "team-notes").mkdir()
    for i, ref in enumerate(("55ee14be^", "b0a616d9^")):
        if i == 1:
            (ws / "team-notes" / "auth.md").write_text(NOTES, encoding="utf-8")
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
