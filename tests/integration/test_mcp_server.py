"""The MCP server, exercised in-process through the official MCP client."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("mcp")

from mcp import Client  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

import dakc  # noqa: E402
from dakc.cli import app  # noqa: E402
from dakc.mcp_server import create_server  # noqa: E402

from tests.conftest import POSTGRES_DOCS, write_files  # noqa: E402
from tests.integration.test_python_api import paid_workspace  # noqa: E402

READ_TOOLS = {"status", "check_sources", "list_artifacts", "read_artifact", "explain", "trace", "downstream",
              "graph_stats", "builds", "diff_builds", "duplicates", "verify", "conflicts", "links"}


def session(server, *calls: tuple[str, dict[str, Any]]):
    """Run tool calls in one client session; returns the list of CallToolResults."""

    async def go():
        async with Client(server) as c:
            return [await c.call_tool(name, args) for name, args in calls]

    return asyncio.run(go())


def tools(server) -> dict:
    async def go():
        async with Client(server) as c:
            return {t.name: t for t in (await c.list_tools()).tools}

    return asyncio.run(go())


@pytest.fixture
def ws(tmp_path):
    write_files(tmp_path / "knowledge", POSTGRES_DOCS)
    dakc.ingest(tmp_path / "knowledge", tmp_path, provider="mock")
    return tmp_path


def test_tools_and_safety_annotations(ws):
    ts = tools(create_server(ws))
    assert set(ts) == READ_TOOLS | {"compile", "rollback"}
    for name in READ_TOOLS:
        assert ts[name].annotations.read_only_hint is True, name
    assert ts["compile"].annotations.read_only_hint is False and ts["compile"].annotations.destructive_hint is False
    assert ts["rollback"].annotations.destructive_hint is True
    assert ts["compile"].annotations.open_world_hint is False  # a free provider makes no network calls
    assert "artifact" in ts["trace"].input_schema["properties"]
    assert set(tools(create_server(ws, read_only=True))) == READ_TOOLS


def test_answering_a_question_with_provenance(ws):
    found, page, tr = session(
        create_server(ws),
        ("list_artifacts", {"query": "jsonb"}),
        ("read_artifact", {"artifact": "JSONB"}),
        ("trace", {"artifact": "page:jsonb"}),
    )
    assert not found.is_error and found.structured_content["artifacts"][0]["id"] == "page:jsonb"
    assert page.structured_content["text"].startswith("# JSONB") and not page.structured_content["truncated"]
    claim = tr.structured_content["claims"][0]
    assert claim["evidence"][0]["source_path"] == "databases/postgres.md" and claim["evidence"][0]["sentences"]


def test_compile_previews_by_default_then_applies(ws):
    server = create_server(ws)
    write_files(ws / "knowledge", {"databases/redis.md": "Redis supports Clustering.\n"})
    preview, st = session(server, ("compile", {}), ("status", {}))
    p = preview.structured_content
    assert p["applied"] is False and p["plan"]["has_work"] and "apply=true" in p["next"]
    assert st.structured_content["head"] == 1 and not st.structured_content["up_to_date"]  # nothing was built

    applied, again, st = session(server, ("compile", {"apply": True}), ("compile", {}), ("status", {}))
    assert applied.structured_content["applied"] and applied.structured_content["report"]["status"] == "committed"
    assert again.structured_content["next"].startswith("nothing to do")
    assert st.structured_content["head"] == 2 and st.structured_content["up_to_date"]

    rb, bl = session(server, ("rollback", {"to": 1}), ("builds", {}))
    assert rb.structured_content["restored_build"] == 1
    assert [b["status"] for b in bl.structured_content["builds"]][:3] == ["committed", "rolled_back", "committed"]


def test_errors_reach_the_agent_as_tool_errors(ws):
    server = create_server(ws)
    missing, bad_trace, bad_rollback, bad_kind = session(
        server,
        ("read_artifact", {"artifact": "page:nope"}),
        ("trace", {"artifact": "databases/postgres.md"}),
        ("rollback", {"to": 7}),
        ("list_artifacts", {"kind": "pages"}),
    )
    assert missing.is_error and "no source or artifact matches 'page:nope'" in missing.content[0].text
    assert bad_trace.is_error and "page or a claim" in bad_trace.content[0].text
    assert bad_rollback.is_error and "not a committed build" in bad_rollback.content[0].text
    assert bad_kind.is_error  # rejected by the input schema


def test_outputs_are_bounded(ws):
    page, tr, listed, ex = session(
        create_server(ws),
        ("read_artifact", {"artifact": "page:postgresql", "max_chars": 200}),
        ("trace", {"artifact": "page:postgresql", "limit": 1}),
        ("list_artifacts", {"kind": None, "limit": 2}),
        ("explain", {"artifact": "page:postgresql", "limit": 1}),
    )
    assert page.structured_content["truncated"] and len(page.structured_content["text"]) == 200
    t = tr.structured_content
    assert len(t["claims"]) == 1 and t["claims_total"] > 1
    assert len(listed.structured_content["artifacts"]) == 2 and listed.structured_content["more_available"]
    assert len(ex.structured_content["depends_on"]) == 1 and ex.structured_content["depends_on_total"] > 1


def test_llm_cost_guard(tmp_path):
    root = paid_workspace(tmp_path)
    server = create_server(root, max_llm_calls=1)
    assert tools(server)["compile"].annotations.open_world_hint is True
    preview, applied, full_verify, st = session(
        server,
        ("compile", {}),
        ("compile", {"apply": True}),
        ("verify", {"against_full": True}),
        ("status", {}),
    )
    assert not preview.is_error and preview.structured_content["plan"]["planned_llm_calls"]
    assert applied.is_error and "above this server's limit of 1" in applied.content[0].text
    assert full_verify.is_error and "re-pays" in full_verify.content[0].text
    assert st.structured_content["head"] == 0  # nothing was built or spent


def test_bad_workspace(tmp_path):
    with pytest.raises(dakc.WorkspaceNotFound):
        create_server(tmp_path)
    r = CliRunner().invoke(app, ["mcp", "-w", str(tmp_path)])
    assert r.exit_code == 2 and "dakc.toml" in r.output
    r = CliRunner().invoke(app, ["mcp", "-w", str(tmp_path), "--transport", "carrier-pigeon"])
    assert r.exit_code == 2


def test_stdio_transport_end_to_end(ws):
    """`dakc mcp` as a subprocess over stdio: nothing but protocol may reach stdout."""
    import sys

    from mcp import StdioServerParameters

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "dakc", "mcp", "--workspace", str(ws), "--read-only"])
        async with Client(params) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            r = await c.call_tool("status", {})
            return c.server_info, names, r

    info, names, r = asyncio.run(go())
    assert info.name == "dakc" and info.version == dakc.__version__
    assert names == READ_TOOLS
    assert not r.is_error and r.structured_content["head"] == 1


def test_stale_is_explained_as_an_upper_bound(ws):
    write_files(ws / "knowledge", {"databases/redis.md": "Redis supports Clustering.\n"})
    st, ex, src = session(
        create_server(ws),
        ("status", {}),
        ("explain", {"artifact": "page:redis"}),
        ("read_artifact", {"artifact": "knowledge/databases/redis.md"}),
    )
    assert "upper bound" in st.structured_content["stale_note"]
    assert ex.structured_content["status"] == "STALE" and "may or may not change" in ex.structured_content["stale_note"]
    assert src.structured_content["text"] == "Redis supports Clustering.\n"
    assert src.structured_content["metadata"]["changed_since_build"] is True


def test_http_transport_rejects_foreign_host_headers(ws):
    """Bound to loopback, the HTTP transport refuses requests for other hosts (DNS rebinding)."""
    from starlette.testclient import TestClient

    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    codes = {}
    for base in ("http://evil.example", "http://127.0.0.1:8766"):
        app = create_server(ws, read_only=True).streamable_http_app(host="127.0.0.1")  # what `dakc mcp` runs
        with TestClient(app, base_url=base) as client:
            codes[base] = client.post("/mcp", json=init, headers=headers).status_code
    assert codes == {"http://evil.example": 421, "http://127.0.0.1:8766": 200}


def test_conflicts_tool_lists_and_shows_a_finding(tmp_path):
    write_files(tmp_path / "knowledge", {"database.md": "PostgreSQL supports JSONB.\n",
                                         "architecture.md": "PostgreSQL does not support JSONB.\n"})
    dakc.ingest(tmp_path / "knowledge", tmp_path, provider="mock")
    listed, = session(create_server(tmp_path, read_only=True), ("conflicts", {}))
    d = listed.structured_content
    assert d["open"] == 1 and d["conflicts"][0]["kind"] == "conflict"
    shown, = session(create_server(tmp_path, read_only=True), ("conflicts", {"conflict": d["conflicts"][0]["id"]}))
    lines = {loc["path"]: loc["line"] for v in shown.structured_content["values"] for loc in v["locations"]}
    assert lines == {"database.md": 1, "architecture.md": 1}
