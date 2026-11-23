"""`dakc review`: the knowledge diff of a change against a git ref (the GitHub Action's engine)."""

from __future__ import annotations

import subprocess

import pytest

from dakc.review import MARKER, Price, render_markdown, review, run
from dakc.workspace import Workspace

from tests.conftest import POSTGRES_DOCS, write_files


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    write_files(root / "knowledge", POSTGRES_DOCS)
    Workspace.init(root, source_dir="knowledge", provider="heuristic")
    (root / ".gitignore").write_text(".dakc/\nwiki/\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


def edit(root):
    p = root / "knowledge" / "databases" / "postgres.md"
    p.write_text(p.read_text().replace("supports JSONB", "stores JSONB in a binary format"))
    write_files(root / "knowledge", {"queues/nats.md": "# NATS\n\nNATS supports Subjects.\n"})


def test_a_change_is_reported_as_facts_pages_and_cost(repo):
    edit(repo)
    r = review(repo, "main", prices=[Price("model", 1.0, 4.0)])
    assert r.documents["modified"] == ["databases/postgres.md"] and r.documents["new"] == ["queues/nats.md"]
    assert any("stores JSONB in a binary format" in f["text"] for f in r.facts_added)
    assert any("NATS supports Subjects" in f["text"] and f["sources"] == ["queues/nats.md"] for f in r.facts_added)
    assert [f["text"] for f in r.facts_retracted] == ["PostgreSQL supports JSONB."]
    assert r.facts_retracted[0]["sources"] == ["databases/postgres.md"]
    assert "NATS" in r.pages["new"] and r.llm_calls > 0 and r.passages_reextracted == 2
    assert r.costs[0]["usd"] == round((r.input_tokens * 1.0 + r.output_tokens * 4.0) / 1e6, 4)
    md = render_markdown(r)
    assert md.startswith(MARKER) and "Facts added" in md and "~~PostgreSQL supports JSONB.~~" in md
    assert "Pages regenerated" in md and "NATS (new)" in md


def test_the_base_build_is_kept_and_reused(repo):
    edit(repo)
    first = review(repo, "main")
    assert (repo / ".dakc" / "review" / "base.db").exists()
    again = review(repo, "main")  # the base build is already current: same answer
    assert again.to_dict() == first.to_dict()


def test_no_change_and_the_workspace_store_is_untouched(repo):
    r, text = run(repo, "main")
    assert not r.has_changes and "does not affect the knowledge base" in text
    assert not (repo / ".dakc" / "dakc.db").exists()  # review never touches the workspace's own knowledge base


def test_a_committed_branch_is_compared_with_its_base(repo):
    git(repo, "checkout", "-q", "-b", "feature")
    edit(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "edit")
    r, text = run(repo, "main", fmt="json")
    assert r.has_changes and '"facts_retracted"' in text and r.head == subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short=10", "HEAD"], capture_output=True, text=True).stdout.strip()
    worktrees = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout
    assert len(worktrees.strip().splitlines()) == 1  # the temporary base checkout was removed


def test_bad_inputs(repo, tmp_path):
    from dakc.errors import InvalidArgument, WorkspaceNotFound

    with pytest.raises(InvalidArgument, match="no-such-ref"):
        review(repo, "no-such-ref")
    with pytest.raises(InvalidArgument, match="LABEL:INPUT:OUTPUT"):
        run(repo, "main", prices=["cheap"])
    with pytest.raises(WorkspaceNotFound):
        review(tmp_path / "nowhere", "main")


def test_a_change_that_contradicts_another_document_is_flagged_and_its_fix_reported(repo):
    write_files(repo / "knowledge", {"architecture.md": "# Architecture\n\nPostgreSQL does not support JSONB.\n"})
    r = review(repo, "main")
    assert r.conflicts_open == 1 and not r.conflicts_resolved
    (c,) = r.conflicts_introduced
    assert {v["value"]: v["paths"] for v in c["values"]} == {"is negated": ["architecture.md"],
                                                            "is stated": ["databases/postgres.md"]}
    md = render_markdown(r)
    assert "introduce 1 conflict between documents" in md and "<details open><summary>Conflicts introduced (1)" in md

    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "contradiction lands on main")
    (repo / "knowledge" / "architecture.md").write_text("# Architecture\n\nPostgreSQL supports JSONB.\n")
    fixed = review(repo, "main")
    assert [x["id"] for x in fixed.conflicts_resolved] == [c["id"]] and fixed.conflicts_open == 0
    assert "resolve 1 conflict between documents" in render_markdown(fixed)
