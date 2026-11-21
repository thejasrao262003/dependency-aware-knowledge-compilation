"""End-to-end compiler semantics on small, hand-written corpora."""

import shutil

import pytest

from dakc.compiler import Compiler
from dakc.providers import make_provider
from dakc.provenance.compare import canonical_state, diff_states
from dakc.storage import Store
from dakc.transactions import FaultInjector, InjectedFault, state_digest

from tests.conftest import POSTGRES_DOCS, compile_once, write_files


def versions(store: Store) -> dict[str, int]:
    return {r[0]: r[1] for r in store.conn.execute("SELECT id, version FROM artifacts WHERE to_build IS NULL")}


def assert_equals_full_rebuild(src, store, tmp_path, **kw):
    ref = Store(tmp_path / "ref.db")
    compile_once(ref, src, strategy="full", **kw)
    d = diff_states(canonical_state(ref), canonical_state(store))
    ref.close()
    assert d.equal, d.by_kind()


def test_prd_example_only_jsonb_and_overview_are_regenerated(kb, tmp_path):
    """PRD §5: A2 changes -> JSONB page and the PostgreSQL overview are rebuilt;
    Transactions and Indexing pages are untouched even though they come from the
    same paragraph, because their claims are re-extracted with identical ids."""
    src, store = kb
    write_files(src, POSTGRES_DOCS)
    compile_once(store, src)
    before = versions(store)
    write_files(src, {"databases/postgres.md": (
        "# PostgreSQL\n\n"
        "PostgreSQL supports Transactions. PostgreSQL stores JSONB in a binary format. PostgreSQL supports Indexing.\n"
    )})
    rep = compile_once(store, src)
    assert rep.n("reextract") == 1
    assert sorted(rep.ops["regenerate_page"]) == ["page:jsonb", "page:postgresql"]
    assert "page:transactions" not in rep.ops["rerender_page"]
    after = versions(store)
    assert after["page:transactions"] == before["page:transactions"]
    assert after["page:indexing"] == before["page:indexing"]
    assert after["page:redis"] == before["page:redis"]
    assert after["page:jsonb"] == before["page:jsonb"] + 1
    assert rep.usage.llm_calls == 1 + 2 + rep.n("regenerate_summary")
    assert_equals_full_rebuild(src, store, tmp_path)


def test_unchanged_corpus_is_a_noop(kb):
    src, store = kb
    write_files(src, POSTGRES_DOCS)
    compile_once(store, src)
    digest, head = state_digest(store), store.head
    rep = compile_once(store, src)
    assert rep.status == "noop" and rep.usage.calls == 0
    assert state_digest(store) == digest and store.head == head


def test_deletion_retracts_only_unsupported_claims(kb, tmp_path):
    src, store = kb
    write_files(src, {
        **POSTGRES_DOCS,
        "databases/copy.md": "Redis supports Replication.\n",  # also stated in redis.md
    })
    compile_once(store, src)
    (src / "databases/redis.md").unlink()
    rep = compile_once(store, src)
    # "Redis stores data in memory" only lived in redis.md -> retracted.
    assert store.current_artifact("clm:" + __import__("dakc.hashing", fromlist=["short"]).short("redis stores data in memory")) is None
    # "Redis supports Replication" is still supported by copy.md -> survives.
    survivor = [c for c in rep.ops.get("claim_provenance_changed", [])]
    assert survivor, "shared claim should lose one support but stay valid"
    page = store.current_artifact("page:redis")[0]
    assert "databases/redis.md" not in page.rendered and "databases/copy.md" in page.rendered
    for row in store.conn.execute("SELECT payload FROM artifacts WHERE kind='claim' AND to_build IS NULL"):
        assert '"supports": []' not in row[0]
    assert_equals_full_rebuild(src, store, tmp_path)


def test_rename_costs_no_llm_calls_and_keeps_source_id(kb, tmp_path):
    src, store = kb
    write_files(src, POSTGRES_DOCS)
    compile_once(store, src)
    (src / "messaging/kafka.md").rename(src / "messaging/kafka-notes.md")
    rep = compile_once(store, src)
    assert rep.scan["renamed"] == 1 and rep.usage.calls == 0
    assert store.current_source("src:messaging-kafka-md").path == "messaging/kafka-notes.md"
    assert rep.n("regenerate_page") == 0 and rep.n("rerender_page") >= 1
    assert_equals_full_rebuild(src, store, tmp_path)


def test_duplicate_source_costs_no_extraction(kb, tmp_path):
    src, store = kb
    write_files(src, POSTGRES_DOCS)
    compile_once(store, src)
    shutil.copy(src / "databases/redis.md", src / "databases/redis-copy.md")
    rep = compile_once(store, src)
    assert rep.usage.cache_hits >= 1 and rep.usage.by_stage.get("extract", 0) == 0
    assert rep.n("regenerate_page") == 0  # knowledge unchanged; citations re-rendered
    assert_equals_full_rebuild(src, store, tmp_path)


def test_entity_merge_via_alias_config(kb, tmp_path):
    src, store = kb
    write_files(src, {"a.md": "Postgres supports Partitioning.\n", "b.md": "PostgreSQL supports Partitioning well.\n"})
    compile_once(store, src)
    assert store.current_artifact("page:postgres") and store.current_artifact("page:postgresql")
    aliases = {"postgres": "PostgreSQL"}
    rep = compile_once(store, src, aliases=aliases)
    assert rep.usage.by_stage.get("extract", 0) == 0, "alias changes never re-extract"
    assert store.current_artifact("page:postgres") is None
    page = store.current_artifact("page:postgresql")[0]
    assert len(page.claim_ids) == 2
    assert_equals_full_rebuild(src, store, tmp_path, aliases=aliases)


@pytest.mark.parametrize("edit", ["modify", "delete", "add", "rename"])
def test_dry_run_with_free_provider_is_exact(kb, edit):
    src, store = kb
    write_files(src, POSTGRES_DOCS)
    compile_once(store, src)
    if edit == "modify":
        write_files(src, {"databases/redis.md": "Redis supports Clustering. Redis supports Replication.\n"})
    elif edit == "delete":
        (src / "messaging/kafka.md").unlink()
    elif edit == "add":
        write_files(src, {"messaging/nats.md": "NATS streams events to Kafka.\n"})
    else:
        (src / "databases/redis.md").rename(src / "databases/redis2.md")
    digest = state_digest(store)
    plan = compile_once(store, src, dry_run=True)
    assert state_digest(store) == digest, "dry run must not modify anything"
    assert plan.exact
    actual = compile_once(store, src)
    assert {k: sorted(v) for k, v in plan.ops.items()} == {k: sorted(v) for k, v in actual.ops.items()}
    assert plan.usage.llm_calls == actual.usage.llm_calls


def test_dry_run_with_llm_provider_is_an_upper_bound_without_calls(kb):
    src, store = kb
    write_files(src, POSTGRES_DOCS)
    compile_once(store, src)
    write_files(src, {"databases/redis.md": "Redis supports Clustering.\n"})

    class Boom:
        free = False
        is_llm = True
        fingerprint = "heuristic-extractor/2"  # same fingerprint: cache keys line up

        def extract(self, *a, **k):
            raise AssertionError("dry run must not call a paid provider")

    bundle = make_provider("mock")
    bundle.extractor = Boom()
    plan, _ = Compiler(store, bundle, src, dry_run=True).run()
    assert not plan.exact and plan.planned_llm_calls["extract"] == 1


@pytest.mark.parametrize("fault", ["extract:1", "page:1", "summary:1", "commit:1", "commit:20"])
def test_failure_injection_preserves_previous_state_and_retry_converges(kb, tmp_path, fault):
    src, store = kb
    write_files(src, POSTGRES_DOCS)
    compile_once(store, src)
    digest = state_digest(store)
    write_files(src, {"databases/postgres.md": "PostgreSQL supports Partitioning. PostgreSQL supports JSONB.\n"})
    (src / "messaging/kafka.md").unlink()
    with pytest.raises(InjectedFault):
        compile_once(store, src, fault=FaultInjector(fault))
    assert state_digest(store) == digest
    rep = compile_once(store, src)
    assert rep.status == "committed"
    assert_equals_full_rebuild(src, store, tmp_path)


def test_crashed_build_is_marked_aborted(kb):
    src, store = kb
    write_files(src, POSTGRES_DOCS)
    bid = store.begin_build("dakc", "compile")  # simulate a process that died mid-build
    compile_once(store, src)
    assert store.build(bid).status == "aborted"


def test_concurrent_provider_calls_give_identical_results(tmp_path):
    from benchmarks.corpus import generate

    generate(tmp_path / "c", 40)
    a, b = Store(tmp_path / "a.db"), Store(tmp_path / "b.db")
    compile_once(a, tmp_path / "c", jobs=1)
    compile_once(b, tmp_path / "c", jobs=8)
    assert diff_states(canonical_state(a), canonical_state(b)).equal


def test_process_killed_mid_commit_leaves_previous_build_intact(tmp_path):
    """A real crash (os._exit inside the commit transaction), not an exception."""
    import subprocess
    import sys

    from dakc.workspace import Workspace

    write_files(tmp_path / "knowledge", POSTGRES_DOCS)
    Workspace.init(tmp_path, provider="mock")
    dakc = [sys.executable, "-m", "dakc"]
    assert subprocess.run(dakc + ["compile", "-w", str(tmp_path)], capture_output=True).returncode == 0
    store = Store(tmp_path / ".dakc" / "dakc.db")
    digest, head = state_digest(store), store.head
    store.close()

    write_files(tmp_path / "knowledge", {"databases/postgres.md": "PostgreSQL supports Partitioning.\n"})
    killed = subprocess.run(dakc + ["compile", "-w", str(tmp_path), "--fault", "commit:2:exit"], capture_output=True)
    assert killed.returncode == 137

    store = Store(tmp_path / ".dakc" / "dakc.db")
    assert state_digest(store) == digest and store.head == head
    running = [b for b in store.builds() if b.status == "running"]
    assert len(running) == 1
    rep = compile_once(store, tmp_path / "knowledge")
    assert store.build(running[0].id).status == "aborted"
    assert rep.status == "committed"
