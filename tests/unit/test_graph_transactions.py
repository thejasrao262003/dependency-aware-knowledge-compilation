import json

import pytest

from dakc.graph import build_graph, downstream, execution_order, export, find_cycle, layering_violations, resolve_id, upstream
from dakc.transactions import ChangeSet, FaultInjector, InjectedFault, RollbackError, commit, rollback, state_digest

from tests.conftest import POSTGRES_DOCS, compile_once, write_files


def test_downstream_and_upstream_traversal(kb):
    src, store = kb
    write_files(src, POSTGRES_DOCS)
    compile_once(store, src)
    sid = resolve_id(store, "databases/postgres.md")
    down = downstream(store, sid)
    kinds = {i.split(":")[0] for i in down}
    assert {"psg", "clm", "ent", "page", "sum", "index"} <= kinds
    assert "page:jsonb" in down and "page:redis" not in down
    up = upstream(store, "page:jsonb")
    assert sid in up and "src:databases-redis-md" not in up


def test_resolve_friendly_ids(kb):
    src, store = kb
    write_files(src, POSTGRES_DOCS)
    compile_once(store, src)
    assert resolve_id(store, "page:postgre-sql") == "page:postgresql"
    assert resolve_id(store, "entity_postgresql") == "ent:postgresql"
    assert resolve_id(store, "PostgreSQL") == "page:postgresql"
    assert resolve_id(store, "source:databases/redis.md") == "src:databases-redis-md"
    assert resolve_id(store, "nope") is None


def test_graph_is_layered_acyclic_and_exportable(kb):
    src, store = kb
    write_files(src, POSTGRES_DOCS)
    compile_once(store, src)
    assert layering_violations(store) == []
    g = build_graph(store, with_titles=True)
    assert find_cycle(g) is None
    order = execution_order(g)
    assert all(i.startswith("src:") for i in order[0])
    assert order[-1] == ["index:main"]
    rf = json.loads(export(g, "reactflow"))
    assert {"nodes", "edges"} <= rf.keys() and rf["nodes"]
    assert export(g, "dot").startswith("digraph")
    assert json.loads(export(g, "json"))["nodes"]


def test_cycle_detection_finds_injected_cycle(kb):
    src, store = kb
    write_files(src, POSTGRES_DOCS)
    compile_once(store, src)
    g = build_graph(store)
    g.add_edge("ent:postgresql", "page:postgresql")  # page -> entity already exists
    assert find_cycle(g) is not None


def test_commit_fault_rolls_back_whole_transaction(kb):
    src, store = kb
    write_files(src, POSTGRES_DOCS)
    compile_once(store, src)
    before = state_digest(store)
    write_files(src, {"databases/postgres.md": "PostgreSQL supports Partitioning.\n"})
    with pytest.raises(InjectedFault):
        compile_once(store, src, fault=FaultInjector("commit:3"))
    assert state_digest(store) == before
    assert store.builds()[0].status == "failed"


def test_rollback_restores_exact_state_and_rejects_bad_targets(kb):
    src, store = kb
    write_files(src, POSTGRES_DOCS)
    compile_once(store, src)
    d1, head1 = state_digest(store), store.head
    write_files(src, {"databases/postgres.md": "PostgreSQL supports Partitioning.\n"})
    compile_once(store, src)
    assert state_digest(store) != d1
    new = rollback(store, head1)
    assert state_digest(store) == d1 and store.head == new
    with pytest.raises(RollbackError):
        rollback(store, 999)
    # A later compile after rollback re-applies the source change cleanly.
    compile_once(store, src)
    assert store.current_artifact("page:partitioning") is not None


def test_empty_changeset_commit_advances_head(kb):
    _, store = kb
    b = store.begin_build("dakc", "compile")
    commit(store, b, ChangeSet(), {}, {"x": 1})
    assert store.head == b and store.build(b).status == "committed"


def test_fault_injector_spec():
    f = FaultInjector("extract:2,commit:10")
    f.tick("extract")
    with pytest.raises(InjectedFault):
        f.tick("extract")
    f.tick("page")  # untargeted points never fire
