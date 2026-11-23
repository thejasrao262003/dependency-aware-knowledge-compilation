"""Links between documents across compiles: a renamed heading or a moved file breaks links,
the finding names the fix, the fix closes it, and the stored findings always equal a check
from scratch, through edits, renames, deletes and rollback. No model is ever called."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

import dakc
from dakc.cli import app

from tests.conftest import write_files

CACHING = "# Caching\n\n## Redis setup\n\nGateway uses Redis for rate limiting.\n\n## Eviction\n\nRedis evicts keys with LRU.\n"
GATEWAY = "# Gateway\n\nSee [Redis setup](caching.md#redis-setup) for the store, and [eviction](caching.md#eviction).\n"


def workspace(root: Path, files: dict[str, str]) -> Path:
    write_files(root / "knowledge", files)
    (root / "dakc.toml").write_text('[dakc]\nsource_dir = "knowledge"\nprovider = "mock"\n', encoding="utf-8")
    return root


def from_scratch_ok(kb) -> bool:
    return kb.verify().checks["broken-link findings equal a check from scratch"]


def test_a_renamed_heading_then_the_fix(tmp_path):
    ws = workspace(tmp_path, {"caching.md": CACHING, "gateway.md": GATEWAY})
    with dakc.open(ws) as kb:
        rep = kb.compile()
        assert rep.counts["links_broken"] == 0 and kb.links() == []
        assert [(x["path"], x["anchor"]) for x in kb.linked_from("caching.md")] == [("gateway.md", "redis-setup"),
                                                                                  ("gateway.md", "eviction")]
        (ws / "knowledge" / "caching.md").write_text(CACHING.replace("## Redis setup", "## Rate-limit store"), encoding="utf-8")
        rep = kb.compile()
        assert rep.usage.llm_calls == 0  # a heading rename re-extracts nothing
        (b,) = kb.links()
        assert rep.ops["link_broken"] == [b.id] and rep.counts["links_broken"] == 1
        assert (b.kind, b.path, b.line, b.href) == ("missing_anchor", "gateway.md", 3, "caching.md#redis-setup")
        assert b.suggestion == "caching.md#rate-limit-store" and '"Redis setup" is now "Rate-limit store"' in b.reason
        assert kb.status().links_broken == 1 and from_scratch_ok(kb)
        (ws / "knowledge" / "gateway.md").write_text(GATEWAY.replace("#redis-setup", "#rate-limit-store"), encoding="utf-8")
        rep = kb.compile()
        assert rep.ops["link_fixed"] == [b.id] and kb.links() == [] and from_scratch_ok(kb)


def test_a_moved_and_a_deleted_document(tmp_path):
    ws = workspace(tmp_path, {"caching.md": CACHING, "gateway.md": GATEWAY})
    with dakc.open(ws) as kb:
        kb.compile()
        (ws / "knowledge" / "caching.md").rename(ws / "knowledge" / "ops-caching.md")
        kb.compile()
        found = kb.links()
        assert {b.kind for b in found} == {"missing_document"} and len(found) == 2
        assert {b.suggestion for b in found} == {"ops-caching.md#redis-setup", "ops-caching.md#eviction"}
        assert from_scratch_ok(kb)
        (ws / "knowledge" / "ops-caching.md").unlink()
        kb.compile()
        assert all(b.kind == "missing_document" and b.suggestion == "" for b in kb.links()) and from_scratch_ok(kb)
        (ws / "knowledge" / "gateway.md").unlink()  # the linking document goes: its findings go with it
        kb.compile()
        assert kb.links() == [] and from_scratch_ok(kb)


def test_rollback_and_a_store_from_before_link_checking(tmp_path):
    ws = workspace(tmp_path, {"caching.md": CACHING, "gateway.md": GATEWAY})
    with dakc.open(ws) as kb:
        first = kb.compile()
        (ws / "knowledge" / "caching.md").write_text(CACHING.replace("## Eviction", "## Expiry"), encoding="utf-8")
        kb.compile()
        assert len(kb.links()) == 1
        kb.rollback(first.build_id)
        assert kb.links() == []  # the findings of build 1
        kb.compile()  # the sources still have the rename: found again, from a rebuilt index
        assert len(kb.links()) == 1 and from_scratch_ok(kb)
        kb.store.conn.execute("DELETE FROM links")
        kb.store.conn.execute("DELETE FROM link_index")
        kb.store.conn.execute("DELETE FROM meta WHERE key='links_fp'")
        assert any("have not been checked" in r for r in kb.status().outdated)
        rep = kb.compile()
        assert rep.status == "committed" and len(kb.links()) == 1 and kb.compile().status == "noop"


def test_the_cli_and_rest(tmp_path):
    ws = workspace(tmp_path, {"caching.md": CACHING.replace("## Redis setup", "## Store"), "gateway.md": GATEWAY})
    run = CliRunner().invoke
    assert run(app, ["compile", "-w", str(ws)]).exit_code == 0
    out = run(app, ["links", "-w", str(ws)])
    assert "gateway.md:3" in out.output and "1 broken link" in out.output
    assert run(app, ["links", "-w", str(ws), "--check"]).exit_code == 1
    assert json.loads(run(app, ["links", "-w", str(ws), "--json"]).output)["broken"] == 1
    assert "gateway.md:3" in run(app, ["links", "caching.md", "--to", "-w", str(ws)]).output
    assert "1 broken" in run(app, ["status", "-w", str(ws)]).output

    from fastapi.testclient import TestClient

    from dakc.api import create_app

    client = TestClient(create_app(ws), base_url="http://localhost")
    assert client.get("/status").json()["links_broken"] == 1
    assert client.get("/links", params={"path": "caching.md"}).json()[0]["href"] == "caching.md#redis-setup"
    assert len(client.get("/links/to", params={"path": "caching.md"}).json()) == 2


def test_the_pull_request_comment_reports_a_link_the_change_breaks(tmp_path):
    from dakc.review import render_markdown, review

    ws = workspace(tmp_path, {"caching.md": CACHING, "gateway.md": GATEWAY})
    git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a],  # noqa: E731
                                    cwd=ws, check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    (ws / "knowledge" / "caching.md").write_text(CACHING.replace("## Redis setup", "## Rate-limit store"), encoding="utf-8")
    r = review(ws, "main")
    assert [x["href"] for x in r.links_broken] == ["caching.md#redis-setup"] and r.links_broken_total == 1
    comment = render_markdown(r)
    assert "break 1 link between documents" in comment and "Fix: `caching.md#rate-limit-store`" in comment
