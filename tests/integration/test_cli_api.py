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
    client = TestClient(create_app(ws))
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
