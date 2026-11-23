"""The Python API (``dakc.open`` / ``KnowledgeBase``): results, errors, locking, read-only plans."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import dakc
from dakc.transactions import state_digest
from dakc.transactions.lock import workspace_lock

from tests.conftest import POSTGRES_DOCS, write_files

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def kb(tmp_path):
    write_files(tmp_path / "knowledge", POSTGRES_DOCS)
    report = dakc.ingest(tmp_path / "knowledge", tmp_path, provider="mock")
    assert report.status == "committed"
    with dakc.open(tmp_path) as kb:
        yield kb


def paid_workspace(root: Path) -> Path:
    """A workspace whose provider is a (fake) LLM client: not free, so cost guards apply."""
    plugins = root / "plugins"
    plugins.mkdir(parents=True)
    shutil.copy(FIXTURES / "fake_custom_client.py", plugins / "byo_client.py")
    write_files(root / "knowledge", POSTGRES_DOCS)
    (root / "dakc.toml").write_text(
        '[dakc]\nsource_dir = "knowledge"\nprovider = "custom"\njobs = 2\n\n'
        '[provider.custom]\nfactory = "byo_client:make"\npath = "plugins"\n',
        encoding="utf-8",
    )
    return root


def test_public_surface():
    from importlib.metadata import version

    assert dakc.__version__ == version("dakc")  # source and package metadata agree
    for name in dakc.__all__:
        assert hasattr(dakc, name), name
    assert issubclass(dakc.WorkspaceNotFound, FileNotFoundError) and issubclass(dakc.ArtifactNotFound, LookupError)
    for err in (dakc.WorkspaceNotFound, dakc.ArtifactNotFound, dakc.BuildLocked, dakc.CompileFailed,
                dakc.CostNotAllowed, dakc.InvalidArgument, dakc.RollbackError, dakc.ConcurrentBuildError):
        assert issubclass(err, dakc.DakcError)


def test_open_requires_a_workspace(tmp_path):
    with pytest.raises(dakc.WorkspaceNotFound, match="dakc.toml"):
        dakc.open(tmp_path)
    with pytest.raises(dakc.InvalidArgument):
        dakc.ingest(tmp_path / "missing", tmp_path)


def test_init_then_compile(tmp_path):
    write_files(tmp_path / "docs", POSTGRES_DOCS)
    with dakc.init(tmp_path, source_dir="docs") as kb:
        assert kb.head == 0 and kb.source_root == (tmp_path / "docs").resolve()
        rep = kb.compile()
        assert rep.status == "committed" and rep.has_work() and kb.head == rep.build_id
        assert (tmp_path / "wiki" / "pages" / "jsonb.md").exists() and rep.projection["written"] > 0
        again = kb.compile()
        assert again.status == "noop" and not again.has_work()


def test_status_check_and_plan_are_read_only(kb):
    st = kb.status()
    assert st.up_to_date and st.head == 1 and st.counts["page"] > 0 and st.stale == {}
    write_files(kb.source_root, {"databases/postgres.md": "PostgreSQL supports Transactions. PostgreSQL stores JSONB compactly. PostgreSQL supports Indexing.\n"})
    ch = kb.check()
    assert ch.modified == ["databases/postgres.md"] and ch.has_changes and ch.to_dict()["unchanged"] == 2
    st = kb.status()
    assert not st.up_to_date and st.stale.get("page", 0) >= 2

    digest, head = state_digest(kb.store), kb.head
    wiki = {p: p.read_bytes() for p in kb.output_root.rglob("*.md")}
    plan = kb.plan()
    assert plan.dry_run and plan.has_work() and plan.build_id is None
    assert state_digest(kb.store) == digest and kb.head == head
    assert {p: p.read_bytes() for p in kb.output_root.rglob("*.md")} == wiki

    rep = kb.compile()
    assert sorted(rep.ops["regenerate_page"]) == sorted(plan.ops["regenerate_page"]) == ["page:jsonb", "page:postgresql"]
    assert kb.status().up_to_date


def test_artifacts_listing(kb):
    pages = kb.artifacts("page")
    assert {a.kind for a in pages} == {"page"} and "page:jsonb" in {a.id for a in pages}
    assert [a.id for a in kb.artifacts("page", query="jsonb")] == ["page:jsonb"]
    assert [a.id for a in kb.artifacts("page", query="JSONB")] == ["page:jsonb"]  # case-insensitive
    assert len(kb.artifacts(limit=3)) == 3
    assert kb.artifacts(limit=None)[0].kind == "source"  # ordered by layer
    assert {a.title for a in kb.artifacts("source")} == set(POSTGRES_DOCS)
    with pytest.raises(dakc.InvalidArgument):
        kb.artifacts("pages")


def test_read_page_claim_and_source(kb):
    page = kb.read("JSONB")  # by title
    assert page.id == "page:jsonb" and page.kind == "page" and page.text.startswith("# JSONB")
    assert page.version == 1 and page.built_in == 1 and "claim_ids" in page.metadata
    claim_id = page.metadata["claim_ids"][0]
    claim = kb.read(claim_id)
    assert claim.kind == "claim" and "JSONB" in claim.text
    assert claim.metadata["supports"][0]["passage_id"].startswith("psg:")
    src = kb.read("databases/postgres.md")
    assert src.kind == "source" and src.metadata["path"] == "databases/postgres.md"
    json.dumps([page.to_dict(), claim.to_dict(), src.to_dict()])


def test_explain_trace_and_errors(kb):
    exp = kb.explain("page:jsonb")
    assert exp.status == "VALID" and any(s["path"] == "databases/postgres.md" for s in exp.upstream_sources)
    t = kb.trace("page:jsonb")
    assert isinstance(t, dakc.PageTrace) and t.claims and t.claims[0].evidence[0].source_path == "databases/postgres.md"
    ct = kb.trace(t.claims[0].claim_id)
    assert isinstance(ct, dakc.ClaimTrace) and ct.to_dict()["evidence"]
    with pytest.raises(dakc.ArtifactNotFound):
        kb.explain("page:nope")
    with pytest.raises(dakc.ArtifactNotFound):
        kb.read("nothing like this")
    with pytest.raises(dakc.InvalidArgument, match="page or a claim"):
        kb.trace("databases/postgres.md")


def test_graph_downstream_and_export(kb):
    d = kb.downstream("databases/postgres.md")
    pages = {a.id for a in d.by_kind["page"]}
    assert {"page:jsonb", "page:postgresql"} <= pages and "page:redis" not in pages
    assert d.total == sum(d.to_dict()["counts"].values())
    assert kb.graph_stats()["nodes"]["source"] == 3
    rf = json.loads(kb.export_graph("reactflow", around="databases/postgres.md"))
    assert any(n["id"] == "page:jsonb" for n in rf["nodes"])
    assert kb.export_graph("dot").startswith("digraph dakc")
    with pytest.raises(dakc.InvalidArgument):
        kb.export_graph("svg")


def test_builds_diff_duplicates_verify(kb):
    write_files(kb.source_root, {"databases/redis.md": "Redis supports Clustering.\n"})
    kb.compile()
    assert [b.status for b in kb.builds()] == ["committed", "committed"]
    d = kb.diff(1)
    assert d.b == 2 and "page:redis" in {i for i, _ in d.changes["page"]}
    assert d.to_dict(limit=1)["counts"]["page"] >= 1
    with pytest.raises(dakc.InvalidArgument):
        kb.diff(99)
    assert all(isinstance(p, dakc.DuplicatePair) for p in kb.duplicates())
    v = kb.verify(against_full=True)
    assert v.ok and v.equal_to_full_rebuild is True and "ok" in v.to_dict()
    assert "equal_to_full_rebuild" not in kb.verify().to_dict()


def test_failed_compile_raises_and_preserves_state(kb):
    write_files(kb.source_root, {"databases/new.md": "CockroachDB extends PostgreSQL.\n"})
    digest, head = state_digest(kb.store), kb.head
    with pytest.raises(dakc.CompileFailed) as info:
        kb.compile(fault="commit:2")
    assert info.value.preserved and info.value.head == head
    assert state_digest(kb.store) == digest and kb.builds()[0].status == "failed"
    assert kb.compile().status == "committed"


def test_compile_respects_the_build_lock(kb):
    with workspace_lock(kb.workspace.db_path):
        with pytest.raises(dakc.BuildLocked):
            kb.compile()
        with pytest.raises(dakc.BuildLocked):
            kb.rollback()
        assert kb.plan().dry_run  # reads never take the lock


def test_rollback(kb):
    with pytest.raises(dakc.RollbackError):
        kb.rollback()  # only one committed build
    write_files(kb.source_root, {"databases/new.md": "CockroachDB extends PostgreSQL.\n"})
    kb.compile()
    assert (kb.output_root / "pages" / "cockroachdb.md").exists()
    r = kb.rollback()
    assert r.restored_build == 1 and r.new_build == kb.head and r.projection["deleted"] >= 1
    assert not (kb.output_root / "pages" / "cockroachdb.md").exists()
    assert "rolled_back" in {b.status for b in kb.builds()}


def test_cost_guard_for_llm_providers(tmp_path):
    root = paid_workspace(tmp_path)
    with dakc.open(root) as kb:
        assert not kb.provider().free
        plan = kb.plan()
        assert plan.llm_calls() > 0 and not plan.exact  # an upper bound, nothing spent
        assert kb.head == 0
        kb.compile()
        with pytest.raises(dakc.CostNotAllowed):
            kb.verify(against_full=True)


def test_context_manager_closes_the_connection(tmp_path):
    write_files(tmp_path / "knowledge", POSTGRES_DOCS)
    dakc.ingest(tmp_path / "knowledge", tmp_path)
    with dakc.open(tmp_path) as kb:
        conn = kb.store.conn
    assert kb._store is None
    with pytest.raises(Exception):
        conn.execute("SELECT 1")


def test_references_resolve_forgivingly_and_errors_suggest(kb):
    # a path given relative to another directory resolves when exactly one source matches
    assert kb.resolve("knowledge/databases/postgres.md") == kb.resolve("databases/postgres.md")
    assert kb.resolve("./databases/postgres.md") == kb.resolve("databases/postgres.md")
    with pytest.raises(dakc.ArtifactNotFound, match="did you mean 'databases/postgres.md'"):
        kb.read("databases/postgre.md")
    with pytest.raises(dakc.ArtifactNotFound, match="did you mean 'JSONB'"):
        kb.explain("JSONBB")
    with pytest.raises(dakc.ArtifactNotFound, match="content-addressed"):
        kb.read("psg:0123456789abcdef")


def test_passages_match_on_full_text(kb):
    write_files(kb.source_root, {"notes/long.md": "Kafka " + "padding words " * 10 + "mentions a rare phrase quietly.\n"})
    kb.compile()
    assert [a.title[:5] for a in kb.artifacts("passage", query="rare phrase")] == ["Kafka"]
    assert kb.artifacts("page", query="rare phrase") == []  # pages match on their title only


def test_read_source_returns_the_current_document(kb):
    src = kb.read("databases/postgres.md")
    assert src.text.startswith("# PostgreSQL") and src.metadata["changed_since_build"] is False
    write_files(kb.source_root, {"databases/postgres.md": "PostgreSQL supports Partitioning.\n"})
    src = kb.read("databases/postgres.md")
    assert src.text == "PostgreSQL supports Partitioning.\n" and src.metadata["changed_since_build"] is True
    (kb.source_root / "databases" / "postgres.md").unlink()
    src = kb.read("databases/postgres.md")
    assert src.text == "" and src.metadata["on_disk"].startswith("missing")


def test_neighborhood_is_connected_and_capped(kb):
    nb = kb.neighborhood("page:postgresql", up=6, down=6, per_kind=2)
    by_kind = {}
    for n in nb.nodes:
        by_kind.setdefault(n["kind"], []).append(n)
    assert all(len(v) <= 2 for k, v in by_kind.items() if k != "page")
    assert nb.hidden.get("claim", 0) >= 1  # postgresql has more than two claims
    ids = {n["id"] for n in nb.nodes}
    assert all(e["dependency"] in ids and e["dependent"] in ids for e in nb.edges)
    # every shown node is connected to the root through shown nodes
    adj = {}
    for e in nb.edges:
        adj.setdefault(e["dependency"], set()).add(e["dependent"])
        adj.setdefault(e["dependent"], set()).add(e["dependency"])
    seen, todo = {"page:postgresql"}, ["page:postgresql"]
    while todo:
        for o in adj.get(todo.pop(), ()):
            if o not in seen:
                seen.add(o)
                todo.append(o)
    assert seen == ids


def test_status_reports_work_that_is_not_a_source_change(kb, monkeypatch):
    from dakc import versions

    # DAKC's own logic changed (an upgrade): a compile has work although no source did
    monkeypatch.setattr(versions, "SUMMARY", "summary/test")
    st = kb.status()
    assert not st.up_to_date and st.source_changes["modified"] == 0
    assert st.outdated == ["summary structure logic changed (summary/2 -> summary/test)"]
    assert any("summary structure logic changed" in n for n in kb.plan().notes)  # the compile re-checks that layer
    assert kb.compile().status == "committed"  # and records the new version, even if nothing differs
    assert kb.status().up_to_date and kb.status().outdated == []
    monkeypatch.undo()
    kb.compile()

    # the configuration changed
    cfg = kb.root / "dakc.toml"
    cfg.write_text(cfg.read_text() + '"Postgres" = "PostgreSQL"\n')
    with dakc.open(kb.root) as kb2:
        assert kb2.status().outdated == ["configuration changed (entity aliases, entity resolution or provenance mode)",
                                         "conflict detection, its [conflicts] scope rules or the aliases changed"]
        kb2.compile()
        assert kb2.status().up_to_date


def test_status_reports_a_changed_provider_and_no_false_alarm_after_a_rollback(tmp_path):
    root = paid_workspace(tmp_path)
    text = (root / "dakc.toml").read_text()
    (root / "dakc.toml").write_text(text.replace('provider = "custom"', 'provider = "mock"'))
    with dakc.open(root) as kb:
        kb.compile()
        write_files(root / "knowledge", {"queues/nats.md": "# NATS\n\nNATS supports Subjects.\n"})
        kb.compile()
        kb.rollback(1)  # HEAD is now a rollback build, which records no fingerprints of its own
        st = kb.status()
        assert st.outdated == [] and st.new_sources == 1  # only the source on disk differs
    (root / "dakc.toml").write_text(text)  # back to the (fake) LLM provider
    with dakc.open(root) as kb:
        st = kb.status()
        assert not st.up_to_date and {r.split(" (")[0] for r in st.outdated} == {
            "the extractor changed", "the writer changed", "the summarizer changed"}


def test_watch_needs_a_filesystem_source(tmp_path):
    pytest.importorskip("watchdog")
    from typer.testing import CliRunner

    from dakc.cli import app

    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "nothing.py").write_text(
        "class Empty:\n    kind = 'empty'\n    capabilities = frozenset()\n"
        "    def list(self):\n        return []\n    def read(self, item):\n        return b''\n"
        "def make(name, options, root):\n    return Empty()\n")
    (tmp_path / "dakc.toml").write_text(
        '[dakc]\nprovider = "mock"\n\n[[sources]]\nname = "x"\ntype = "custom"\nfactory = "nothing:make"\nplugin_path = "plugins"\n')
    r = CliRunner().invoke(app, ["watch", "-w", str(tmp_path)])
    assert r.exit_code == 2 and "no filesystem source to watch" in r.output
