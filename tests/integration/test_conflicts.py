"""Conflict detection across a knowledge base (RFC 0002): found when sources disagree,
closed when they agree again, never touching a claim or a page, and kept equal to a
detection from scratch through incremental builds, renames, heading edits and rollback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import dakc
from dakc.cli import app
from dakc.conflicts import detect_in_store
from dakc.storage import Store

from tests.conftest import compile_once, write_files

DB = "# Database\n\nPostgreSQL supports JSONB.\n"
ARCH_NO = "# Architecture\n\nSome context first.\n\nPostgreSQL does not support JSONB.\n"
ARCH_YES = "# Architecture\n\nSome context first.\n\nPostgreSQL supports JSONB.\n"


def workspace(root: Path, files: dict[str, str], toml_extra: str = "") -> Path:
    write_files(root / "knowledge", files)
    (root / "dakc.toml").write_text('[dakc]\nsource_dir = "knowledge"\nprovider = "mock"\n\n' + toml_extra, encoding="utf-8")
    return root


def stored(store: Store) -> dict[str, tuple[str, dict]]:
    rows = store.conn.execute("SELECT id, kind, payload FROM conflicts WHERE to_build IS NULL")
    return {i: (k, json.loads(p)) for i, k, p in rows}


def from_scratch(store: Store, rules=None) -> dict[str, tuple[str, dict]]:
    """What detection over every current claim finds: what the incremental index must equal."""
    return {f.id: (f.kind, f.payload()) for f in detect_in_store(store, rules).values()}


def test_the_workflow_detect_explain_edit_resolve(tmp_path):
    ws = workspace(tmp_path, {"database.md": DB, "architecture.md": ARCH_NO})
    with dakc.open(ws) as kb:
        rep = kb.compile()
        assert len(rep.ops["conflict_found"]) == 1 and rep.counts["conflicts_open"] == 1
        (c,) = kb.conflicts()
        assert c.kind == "conflict" and c.accepted is None and c.since_build == rep.build_id

        full = kb.conflict(c.id[:8])  # a unique prefix is enough
        where = {v.value: [(loc.path, loc.line) for loc in v.locations] for v in full.values}
        assert where == {"is stated": [("database.md", 3)], "is negated": [("architecture.md", 5)]}
        assert any(a.id == "page:postgresql" for a in full.affected["page"])
        assert kb.status().conflicts_open == 1
        page_before = kb.read("page:postgresql").text

        # advisory: both claims stay, with their sources, and the page is not changed by detection
        assert "does not support" in page_before and "supports JSONB" in page_before

        (ws / "knowledge" / "architecture.md").write_text(ARCH_YES, encoding="utf-8")
        rep = kb.compile()
        assert rep.ops["conflict_resolved"] == [c.id] and rep.counts["conflicts_open"] == 0
        assert kb.conflicts() == [] and kb.status().conflicts_open == 0
        with pytest.raises(dakc.ArtifactNotFound):
            kb.conflict(c.id)


def test_the_cli(tmp_path):
    ws = workspace(tmp_path, {"database.md": DB, "architecture.md": ARCH_NO})
    run = CliRunner().invoke
    assert run(app, ["compile", "-w", str(ws)]).exit_code == 0
    out = run(app, ["conflicts", "-w", str(ws)])
    assert out.exit_code == 0 and "1 open conflict" in out.output
    assert run(app, ["conflicts", "-w", str(ws), "--check"]).exit_code == 1
    data = json.loads(run(app, ["conflicts", "-w", str(ws), "--json"]).output)
    cid = data["conflicts"][0]["id"]
    assert data["open"] == 1
    shown = run(app, ["explain", cid, "-w", str(ws)])
    assert shown.exit_code == 0 and "architecture.md:5" in shown.output and "database.md:3" in shown.output
    assert "page (" in shown.output
    status = run(app, ["status", "-w", str(ws)])
    assert "1 open" in status.output


@pytest.mark.parametrize("files,kind", [
    # the question that started this: an MVP document beside the current one
    ({"techstack001.md": "# Tech stack\n\nMongoDB is the DB.\n", "techstack.md": "# Tech stack\n\nPostgreSQL is the DB.\n"}, "scoped"),
    ({"techstack.md": "# Tech stack\n\n## Release 1 (MVP)\n\nMongoDB is the DB.\n\n## Production\n\nPostgreSQL is the DB.\n"}, "scoped"),
    ({"a.md": "In release 1, MongoDB was the DB.\n", "b.md": "PostgreSQL is the DB.\n"}, "scoped"),
    ({"stack-a.md": "MongoDB is the DB.\n", "stack-b.md": "PostgreSQL is the DB.\n"}, "conflict"),
    # the long-standing contradiction fixture: both claims are kept on the page, and now flagged
    ({"ops/kafka-a.md": "Kafka retains Events for 7 days.\n", "ops/kafka-b.md": "Kafka retains Events for 3 days.\n"}, "conflict"),
])
def test_database_changes_between_releases_are_not_conflicts(tmp_path, files, kind):
    ws = workspace(tmp_path, files)
    with dakc.open(ws) as kb:
        kb.compile()
        found = kb.conflicts(scoped=True)
        assert [c.kind for c in found] == [kind]
        assert len(kb.conflicts()) == (kind == "conflict")


def test_an_edit_in_place_is_history_not_a_conflict(tmp_path):
    ws = workspace(tmp_path, {"techstack.md": "# Tech stack\n\nMongoDB is the DB.\n"})
    with dakc.open(ws) as kb:
        kb.compile()
        (ws / "knowledge" / "techstack.md").write_text("# Tech stack\n\nPostgreSQL is the DB.\n", encoding="utf-8")
        kb.compile()
        assert kb.conflicts(scoped=True) == []  # the old claim was retracted: builds keep the history


def test_scope_rules_and_accepting_a_conflict(tmp_path):
    files = {"platform/stack.md": "MongoDB is the DB.\n", "stack.md": "PostgreSQL is the DB.\n",
             "ports.md": "Kafka runs on three nodes.\n", "ops.md": "Kafka runs on five nodes.\n"}
    ws = workspace(tmp_path, files)
    with dakc.open(ws) as kb:
        kb.compile()
        assert len(kb.conflicts()) == 2
        kafka = next(c for c in kb.conflicts() if "Kafka" in c.property)
    toml = (ws / "dakc.toml").read_text()
    (ws / "dakc.toml").write_text(
        toml + '[[conflicts.scope]]\npath = "platform/**"\nlabel = "release 1"\n\n'
        f'[[conflicts.accept]]\nid = "{kafka.id}"\nreason = "two clusters"\n', encoding="utf-8")
    with dakc.open(ws) as kb:
        assert any("scope rules" in r for r in kb.status().outdated)
        rep = kb.compile()  # the rules changed: everything is judged again
        assert rep.status == "committed" and rep.counts["conflicts_open"] == 0
        assert kb.conflicts() == [] and kb.status().conflicts_open == 0 and kb.status().up_to_date
        every = {c.id: c for c in kb.conflicts(scoped=True, accepted=True)}
        assert every[kafka.id].accepted == "two clusters"
        assert {c.kind for c in every.values()} == {"conflict", "scoped"}
        assert from_scratch(kb.store, kb.workspace.config.conflict_rules) == stored(kb.store)
        assert kb.verify().checks["conflict findings equal a detection from scratch"]


def test_incremental_detection_equals_detection_from_scratch(kb):
    src, store = kb
    steps = [
        ({"a.md": "# A\n\nKafka runs on three nodes.\n", "b.md": "# B\n\nKafka runs on five nodes.\n", "c.md": DB}, ["conflict"]),
        ({"b.md": "# B\n\nKafka runs on three nodes.\n"}, []),                          # they agree: resolved
        ({"d.md": ARCH_NO}, ["conflict"]),                                                # a new source disagrees
        ({"c.md": "# Database\n\nIntro.\n\n## Release 1\n\nPostgreSQL supports JSONB.\n"}, ["scoped"]),  # a heading scopes it
        ({"-c.md": "archive/c.md"}, ["scoped"]),                                          # renamed into an archive
        ({"-d.md": None}, []),                                                            # the other side deleted
        ({"e.md": "# E\n\nThe primary database is MongoDB.\n", "f.md": "# F\n\nPostgreSQL is the primary database.\n"},
         ["conflict"]),
    ]
    for step, kinds in steps:
        for rel, text in step.items():
            if rel.startswith("-"):
                old = src / rel[1:]
                if text is None:
                    old.unlink()
                else:
                    (src / text).parent.mkdir(parents=True, exist_ok=True)
                    old.rename(src / text)
            else:
                write_files(src, {rel: text})
        compile_once(store, src)
        assert stored(store) == from_scratch(store)
        assert sorted(k for k, _ in stored(store).values()) == kinds, step


def test_rollback_restores_the_findings_of_the_build_it_returns_to(tmp_path):
    ws = workspace(tmp_path, {"database.md": DB, "architecture.md": ARCH_NO})
    with dakc.open(ws) as kb:
        first = kb.compile()
        (ws / "knowledge" / "architecture.md").write_text(ARCH_YES, encoding="utf-8")
        kb.compile()
        assert kb.conflicts() == []
        kb.rollback(first.build_id)
        assert len(kb.conflicts()) == 1  # the findings of build 1
        (ws / "knowledge" / "architecture.md").write_text(ARCH_NO, encoding="utf-8")  # sources match build 1 again
        rep = kb.compile()
        assert rep.counts["conflicts_open"] == 1 and not rep.ops["conflict_found"]
        assert from_scratch(kb.store) == stored(kb.store)


def test_a_dry_run_reports_conflicts_and_writes_none(tmp_path):
    ws = workspace(tmp_path, {"database.md": DB, "architecture.md": ARCH_YES})
    with dakc.open(ws) as kb:
        kb.compile()
        (ws / "knowledge" / "architecture.md").write_text(ARCH_NO, encoding="utf-8")
        plan = kb.plan()
        assert len(plan.ops["conflict_found"]) == 1
        assert kb.conflicts() == [] and kb.store.conn.execute("SELECT COUNT(*) FROM conflicts").fetchone()[0] == 0


def test_a_store_built_before_conflict_detection_gets_it_on_the_next_compile(tmp_path):
    ws = workspace(tmp_path, {"database.md": DB, "architecture.md": ARCH_NO})
    with dakc.open(ws) as kb:
        kb.compile()
        kb.store.conn.execute("DELETE FROM conflicts")
        kb.store.conn.execute("DELETE FROM conflict_index")
        kb.store.conn.execute("DELETE FROM meta WHERE key='conflicts_fp'")
        assert any("has not run" in r for r in kb.status().outdated)
        rep = kb.compile()
        assert rep.status == "committed" and len(rep.ops["conflict_found"]) == 1
        assert kb.compile().status == "noop"


def test_the_web_ui_conflicts_view(tmp_path):
    """What the Conflicts view, its badge and the notices on artifacts read from the server."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from dakc.api import create_app

    ws = workspace(tmp_path, {"database.md": DB, "architecture.md": ARCH_NO})
    with dakc.open(ws) as kb:
        kb.compile()
    client = TestClient(create_app(ws), base_url="http://localhost")
    assert 'data-nav="conflicts"' in client.get("/ui/").text and "conflictDetail" in client.get("/ui/app.js").text
    assert client.get("/status").json()["conflicts_open"] == 1  # the badge
    (listed,) = client.get("/conflicts").json()
    full = client.get(f"/conflicts/{listed['id']}").json()
    assert {loc["line"] for v in full["values"] for loc in v["locations"]} == {3, 5}
    assert any(a["id"] == "page:postgresql" for a in full["affected"]["page"])
    # a page's notice matches the conflict's claims against the page's trace
    traced = {c["claim_id"] for c in client.get("/artifacts/page:postgresql/trace").json()["claims"]}
    assert traced >= {loc["claim_id"] for v in full["values"] for loc in v["locations"]}
    assert client.get("/conflicts/cfl:nope").status_code == 404


# --- per-document scope labels (RFC 0002, "Per-document scope")
SESSION_RULE = '[[conflicts.scope]]\npath = "sessions/**"\nstatus = "past"\nlabel = "{path}"\n'
BCRYPT = "# Auth\n\nThe password hashing algorithm of Auth is Bcrypt.\n"
ARGON2 = "# Auth\n\nThe password hashing algorithm of Auth is Argon2.\n"


def test_session_notes_are_each_their_own_scope(tmp_path, monkeypatch):
    """A decision and its later reversal, in two session notes, are history, not a conflict;
    the per-document expansion never changes the fingerprint, so a new note re-parses only
    its own claims; and the findings always equal a detection from scratch."""
    import dakc.compiler as compiler

    ws = workspace(tmp_path, {"sessions/2026-03-02.md": BCRYPT, "sessions/2026-05-14.md": ARGON2}, SESSION_RULE)
    with dakc.open(ws) as kb:
        rules = kb.workspace.config.conflict_rules
        rep = kb.compile()
        assert rep.counts["conflicts_open"] == 0
        (scoped,) = kb.conflicts(scoped=True)
        assert scoped.kind == "scoped"
        assert {loc.scope for v in scoped.values for loc in v.locations} == {
            "past, 2026, sessions/2026-03-02.md", "past, 2026, sessions/2026-05-14.md"}
        fp = kb.store.get_meta("conflicts_fp")

        parsed: list[str] = []
        real = compiler.index_rows

        def counting(occurrences, *a, **kw):
            parsed.extend(o.path for o, _m, _t in occurrences)
            return real(occurrences, *a, **kw)

        monkeypatch.setattr(compiler, "index_rows", counting)
        write_files(ws / "knowledge", {"sessions/2026-06-01.md": "# Auth\n\nThe password hashing algorithm of Auth is Scrypt.\n"})
        rep = kb.compile()
        assert parsed == ["sessions/2026-06-01.md"]  # only the new note's claims were parsed
        assert kb.store.get_meta("conflicts_fp") == fp and rep.counts["conflicts_open"] == 0
        assert from_scratch(kb.store, rules) == stored(kb.store)

        # two statements in one note are still compared
        write_files(ws / "knowledge", {"sessions/2026-06-01.md": BCRYPT + "\nThe password hashing algorithm of Auth is Scrypt.\n"})
        rep = kb.compile()
        assert rep.counts["conflicts_open"] == 1
        (c,) = kb.conflicts()
        assert {loc.path for v in c.values for loc in v.locations} == {"sessions/2026-06-01.md"}

        # a note moved out of sessions/ is current: against the other notes it is scoped, as before
        (ws / "knowledge" / "current").mkdir()
        (ws / "knowledge" / "sessions" / "2026-06-01.md").rename(ws / "knowledge" / "current" / "auth.md")
        rep = kb.compile()
        assert from_scratch(kb.store, rules) == stored(kb.store)
        assert [c.kind for c in kb.conflicts()] == ["conflict"]  # the two current statements in current/auth.md
        assert kb.verify().checks["conflict findings equal a detection from scratch"]


def test_a_new_detector_version_rejudges_everything_and_touches_nothing_else(tmp_path, monkeypatch):
    """DETECTOR_VERSION is the conflict layer's version: a store judged by an older detector is
    re-indexed in full on the next compile, with no provider call and no artifact rebuilt."""
    import dakc.conflicts as conflicts

    ws = workspace(tmp_path, {"sessions/2026-03-02.md": BCRYPT, "sessions/2026-05-14.md": ARGON2}, SESSION_RULE)
    # conflicts/7 read "{path}" as a literal label shared by both notes, so they conflicted
    monkeypatch.setattr(conflicts, "DETECTOR_VERSION", "conflicts/7")
    monkeypatch.setattr(conflicts.ScopeRule, "label_for", lambda self, path: self.label)
    with dakc.open(ws) as kb:
        assert kb.compile().counts["conflicts_open"] == 1
        pages = {p.id: p.state_hash for p in kb.store.load_state().of("page").values()}
    monkeypatch.undo()
    with dakc.open(ws) as kb:
        assert any("conflict detection" in r for r in kb.status().outdated)
        rep = kb.compile()
        assert rep.status == "committed" and rep.usage.calls == 0 and rep.changes["artifact_versions_written"] == 0
        assert rep.ops["conflict_resolved"] and rep.counts["conflicts_open"] == 0
        assert {p.id: p.state_hash for p in kb.store.load_state().of("page").values()} == pages
        assert from_scratch(kb.store, kb.workspace.config.conflict_rules) == stored(kb.store)
        assert kb.compile().status == "noop"
