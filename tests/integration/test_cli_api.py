"""The PRD Definition-of-Done flow through the CLI, plus the REST API."""

import json

import pytest
from typer.testing import CliRunner

from dakc.cli import app

from tests.conftest import POSTGRES_DOCS, write_files

runner = CliRunner()


def run(*args, ok=True):
    r = runner.invoke(app, [str(a) for a in args])
    if ok:
        assert r.exit_code == 0, r.output
    return r


@pytest.fixture
def ws(tmp_path):
    write_files(tmp_path / "knowledge", POSTGRES_DOCS)
    return tmp_path


def test_definition_of_done_flow(ws):
    # 1-2. point DAKC at a directory and compile
    r = run("ingest", ws / "knowledge", "-w", ws, "--provider", "mock")
    assert "committed" in r.output and (ws / "wiki" / "pages" / "jsonb.md").exists()
    # 3. inspect the graph
    assert "claim" in run("graph", "-w", ws).output
    tree = run("graph", "-w", ws, "--source", "databases/postgres.md").output
    assert "page:jsonb" in tree and "page:redis" not in tree
    rf = json.loads(run("graph", "-w", ws, "--source", "databases/postgres.md", "--format", "reactflow").output)
    assert rf["nodes"] and rf["edges"]
    # 4. modify a source
    write_files(ws / "knowledge", {"databases/postgres.md": "PostgreSQL supports Transactions. PostgreSQL stores JSONB compactly. PostgreSQL supports Indexing.\n"})
    assert "~ databases/postgres.md" in run("check", "-w", ws).output
    stale = json.loads(run("explain", "page:jsonb", "-w", ws, "--json").output)
    assert stale["status"] == "STALE" and "databases/postgres.md" in stale["reasons"][0]
    # 5-6. dry run shows exactly what will be affected, writes nothing
    plan = json.loads(run("compile", "--dry-run", "-w", ws, "--json").output)
    assert sorted(plan["operations"]["regenerate_page"]) == ["page:jsonb", "page:postgresql"]
    assert json.loads(run("explain", "page:jsonb", "-w", ws, "--json").output)["status"] == "STALE"
    # 7. incremental compile
    rep = json.loads(run("compile", "--incremental", "-w", ws, "--json").output)
    assert sorted(rep["operations"]["regenerate_page"]) == sorted(plan["operations"]["regenerate_page"])
    # 8. inspect the changes
    assert "page:jsonb" in run("diff", "1", "-w", ws).output
    assert json.loads(run("explain", "page:jsonb", "-w", ws, "--json").output)["status"] == "VALID"
    # 9. compare with a full rebuild
    assert "PASS  equal to a full rebuild" in run("verify", "--against-full", "-w", ws).output
    # 10. a failed compilation is rolled back; a committed one can be rolled back on demand
    write_files(ws / "knowledge", {"databases/new.md": "CockroachDB extends PostgreSQL.\n"})
    r = run("compile", "-w", ws, "--fault", "commit:2", ok=False)
    assert r.exit_code == 1 and "previous valid compilation preserved: yes" in r.output
    run("compile", "-w", ws)
    assert (ws / "wiki" / "pages" / "cockroachdb.md").exists()
    out = run("rollback", "-w", ws).output
    assert "Restored state of build" in out
    assert not (ws / "wiki" / "pages" / "cockroachdb.md").exists()
    assert "PASS  wiki files match the store" in run("verify", "-w", ws).output
    assert "rolled_back" in run("builds", "-w", ws).output


def test_status_and_errors(ws):
    r = run("status", "-w", ws, ok=False)
    assert r.exit_code == 2 and "dakc.toml" in r.output
    run("ingest", ws / "knowledge", "-w", ws)
    assert "Up to date" in run("status", "-w", ws).output
    (ws / "knowledge" / "messaging" / "kafka.md").unlink()
    assert "Stale" in run("status", "-w", ws).output
    assert run("explain", "page:nope", "-w", ws, ok=False).exit_code == 2
    assert run("compile", "--strategy", "bogus", "-w", ws, ok=False).exit_code == 2


def test_rest_api(ws):
    from fastapi.testclient import TestClient

    from dakc.api import create_app

    run("ingest", ws / "knowledge", "-w", ws)
    client = TestClient(create_app(ws), base_url="http://localhost")
    assert client.get("/status").json()["head"] == 1
    assert client.get("/check").json()["unchanged"] == 3
    exp = client.get("/artifacts/page:jsonb").json()
    assert exp["status"] == "VALID" and exp["kind"] == "page"
    assert "# JSONB" in client.get("/artifacts/page:jsonb/content").text
    g = client.get("/graph", params={"source": "databases/postgres.md"}).json()
    assert any(n["id"] == "page:jsonb" for n in g["nodes"])
    write_files(ws / "knowledge", {"databases/redis.md": "Redis supports Clustering.\n"})
    plan = client.get("/plan").json()
    assert plan["dry_run"] and plan["operation_counts"]["reextract"] == 1
    assert client.get("/builds").json()[0]["status"] in ("committed", "noop")
    assert client.get("/artifacts/page:nope").status_code == 404
    assert client.get("/conflicts").json() == []
    write_files(ws / "knowledge", {"databases/pg-notes.md": "PostgreSQL does not support JSONB.\n"})
    run("compile", "-w", ws)
    (c,) = client.get("/conflicts").json()
    assert c["kind"] == "conflict" and client.get(f"/conflicts/{c['id']}").json()["affected"]["page"]
    assert client.get("/conflicts/cfl:nope").status_code == 404


def test_missing_extra_hint_keeps_brackets(ws, monkeypatch):
    """Rich would swallow `[mcp]` as a markup tag; the install hint must show it."""
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None)
    r = run("mcp", "-w", ws, ok=False)
    assert r.exit_code == 2 and "pip install 'dakc[mcp]'" in r.output


def test_http_servers_are_local_only(ws):
    from fastapi.testclient import TestClient

    from dakc.api import create_app

    run("ingest", ws / "knowledge", "-w", ws)
    for host in ("localhost", "127.0.0.1"):
        assert TestClient(create_app(ws), base_url=f"http://{host}:8765").get("/health").status_code == 200
    # a page served from another domain that resolves to 127.0.0.1 (DNS rebinding) is refused
    assert TestClient(create_app(ws), base_url="http://evil.example").get("/status").status_code == 400
    for cmd in (["serve"], ["mcp", "--transport", "streamable-http"]):
        r = run(*cmd, "-w", ws, "--host", "0.0.0.0", ok=False)
        assert r.exit_code == 2 and "no authentication" in r.output


def test_web_ui_and_its_endpoints(ws):
    from fastapi.testclient import TestClient

    from dakc.api import create_app

    run("ingest", ws / "knowledge", "-w", ws)
    write_files(ws / "knowledge", {"databases/redis.md": "Redis supports Clustering.\n"})
    run("compile", "-w", ws)
    client = TestClient(create_app(ws), base_url="http://localhost", follow_redirects=False)
    assert client.get("/").headers["location"] == "/ui/"
    index = client.get("/ui/")
    assert index.status_code == 200 and '<script type="module" src="app.js">' in index.text
    for asset in ("app.js", "app.css"):
        assert client.get(f"/ui/{asset}").status_code == 200
    page = client.get("/artifacts/page:jsonb/read").json()
    assert page["kind"] == "page" and page["text"].startswith("# JSONB")
    nb = client.get("/artifacts/page:jsonb/neighborhood", params={"up": 6, "down": 6}).json()
    kinds = {n["kind"] for n in nb["nodes"]}
    assert nb["root"] == "page:jsonb" and {"source", "passage", "claim", "page", "index"} <= kinds
    ids = {n["id"] for n in nb["nodes"]}
    assert all(e["dependency"] in ids and e["dependent"] in ids for e in nb["edges"])
    assert client.get("/titles", params=[("id", "page:jsonb"), ("id", "page:nope")]).json() == {"page:jsonb": "JSONB"}
    titled = client.get("/diff/1/2", params={"titles": "true"}).json()
    assert any(x["title"] == "Redis" and x["change"] == "updated" for x in titled["page"])
    assert client.get("/diff/1/2").json() == {k: [[x["id"], x["change"]] for x in v] for k, v in titled.items()}
