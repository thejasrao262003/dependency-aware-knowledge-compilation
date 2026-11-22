"""Publishers (RFC 0001): the ledger, resumable batches, rollbacks, the jsonl feed, wiring."""

from __future__ import annotations

import json

import pytest

import dakc
from dakc.errors import PublishError
from dakc.publishers import PublishBatch, forget, publish
from dakc.storage import Store
from dakc.transactions import rollback

from tests.conftest import POSTGRES_DOCS, compile_once, write_files

KINDS = frozenset({"claim", "page", "summary"})


class Target:
    """A publisher whose target is a dict; it can be told to fail on its n-th call."""

    kind = "memory"
    kinds = KINDS

    def __init__(self, fail_on: int | None = None):
        self.data: dict[str, str] = {}
        self.calls = 0
        self.fail_on = fail_on
        self.sent: list[str] = []

    def publish(self, batch: PublishBatch) -> None:
        self.calls += 1
        if self.fail_on == self.calls:
            raise ConnectionError("target down")
        for d in batch.deletes:
            self.data.pop(d, None)
        for r in batch.upserts:
            self.data[r.id] = r.state_hash
            self.sent.append(r.id)


def current(store: Store, kinds=KINDS) -> dict[str, str]:
    marks = ",".join("?" * len(kinds))
    return dict(store.conn.execute(
        f"SELECT id, state_hash FROM artifacts WHERE to_build IS NULL AND kind IN ({marks})", sorted(kinds)))


@pytest.fixture
def built(kb):
    src, store = kb
    write_files(src, POSTGRES_DOCS)
    compile_once(store, src)
    return src, store


def test_a_first_publish_sends_everything_then_only_changes(built):
    src, store = built
    t = Target()
    r = publish(store, "t", t)
    assert r.ok and r.upserted == len(current(store)) and t.data == current(store)
    calls = t.calls
    r = publish(store, "t", t)
    assert (r.upserted, r.deleted, r.pending) == (0, 0, 0) and t.calls == calls  # nothing to send, nothing sent
    before = current(store)
    (src / "databases" / "postgres.md").write_text(
        (src / "databases" / "postgres.md").read_text().replace("supports JSONB", "stores JSONB in a binary format"))
    compile_once(store, src)
    after = current(store)
    r = publish(store, "t", t)
    changed = {k for k in after if before.get(k) != after[k]}
    assert r.upserted == len(changed) and r.deleted == len(before.keys() - after.keys()) >= 1  # the retracted claim
    assert t.data == after


def test_a_failed_batch_stays_pending_and_the_next_run_resumes(built):
    src, store = built
    t = Target(fail_on=2)
    r = publish(store, "t", t, batch_size=5)
    assert not r.ok and "ConnectionError: target down" in r.error
    assert r.upserted == 5 and r.pending == len(current(store)) - 5
    r = publish(store, "t", t, batch_size=5)
    assert r.ok and r.pending == 0 and t.data == current(store)
    assert len(t.sent) == len(set(t.sent)) == len(current(store))  # nothing acknowledged was sent twice


def test_after_a_rollback_the_target_converges(built):
    src, store = built
    t = Target()
    publish(store, "t", t)
    first = current(store)
    write_files(src, {"queues/kafka.md": "# Kafka\n\nKafka supports Topics. Kafka uses ZooKeeper.\n"})
    compile_once(store, src)
    publish(store, "t", t)
    assert t.data == current(store) != first
    rollback(store, 1)
    r = publish(store, "t", t)
    assert r.ok and r.deleted >= 1 and t.data == current(store) == first


def test_forgetting_the_ledger_republishes_everything(built):
    src, store = built
    t = Target()
    publish(store, "t", t)
    assert forget(store, "t") == len(current(store))
    assert publish(store, "t", Target()).upserted == len(current(store))


def test_records_carry_text_and_source_paths(built):
    src, store = built

    class Keep(Target):
        kinds = frozenset({"claim", "page", "passage"})

        def publish(self, batch):
            self.records = getattr(self, "records", []) + batch.upserts

    k = Keep()
    publish(store, "k", k)
    claim = next(r for r in k.records if r.kind == "claim" and "JSONB" in r.text)
    assert claim.sources == ["databases/postgres.md"] and "PostgreSQL" in claim.metadata["mentions"]
    assert claim.build == 1 and claim.title == claim.text
    page = next(r for r in k.records if r.kind == "page" and r.title == "PostgreSQL")
    assert page.text.startswith("# PostgreSQL") and "databases/postgres.md" in page.sources
    passage = next(r for r in k.records if r.kind == "passage" and "JSONB" in r.text)
    assert passage.sources == ["databases/postgres.md"]


# ------------------------------------------------------------------ via the workspace
def workspace(root, publishers: str):
    write_files(root / "knowledge", POSTGRES_DOCS)
    (root / "dakc.toml").write_text('[dakc]\nsource_dir = "knowledge"\nprovider = "mock"\n\n' + publishers, encoding="utf-8")
    return root


def test_compile_runs_the_jsonl_feed_and_a_failure_does_not_fail_the_build(tmp_path):
    root = workspace(tmp_path, '[[publishers]]\nname = "feed"\ntype = "jsonl"\npath = "out/feed.jsonl"\nkinds = ["claim"]\n')
    (root / "out" / "feed.jsonl").mkdir(parents=True)  # the target is broken: a directory, not a file
    with dakc.open(root) as kb:
        report = kb.compile()
        assert report.status == "committed"
        pub = report.publications[0]
        assert pub["name"] == "feed" and not pub["ok"] and pub["pending"] == kb.status().counts["claim"]
        (root / "out" / "feed.jsonl").rmdir()
        [r] = kb.publish()
        assert r.ok and r.upserted == kb.status().counts["claim"]
        lines = [json.loads(x) for x in (root / "out" / "feed.jsonl").read_text().splitlines()]
        assert {x["op"] for x in lines} == {"upsert"} and {x["kind"] for x in lines} == {"claim"}
        assert all(x["sources"] for x in lines)
        write_files(root / "knowledge", {"databases/postgres.md": "# PostgreSQL\n\nPostgreSQL supports Indexing.\n"})
        report = kb.compile()
        pub = report.publications[0]
        assert pub["ok"] and pub["deleted"] >= 1
        tail = [json.loads(x) for x in (root / "out" / "feed.jsonl").read_text().splitlines()][len(lines):]
        deleted = {x["id"] for x in tail if x["op"] == "delete"}
        retracted = {x["id"] for x in lines if "Transactions" in x["text"] or "JSONB" in x["text"]}
        assert deleted == retracted and len(deleted) == pub["deleted"] == 2  # the two facts the edit removed
        assert kb.publish(dry_run=True)[0].pending == 0


def test_a_bad_publisher_configuration_fails_before_anything_is_built(tmp_path):
    root = workspace(tmp_path, '[[publishers]]\nname = "vec"\ntype = "qdrant"\n')
    with dakc.open(root) as kb:
        with pytest.raises(PublishError, match="unknown publisher type 'qdrant'"):
            kb.compile()
        assert kb.status().head == 0


def test_rollback_also_brings_publishers_back(tmp_path):
    root = workspace(tmp_path, '[[publishers]]\nname = "feed"\ntype = "jsonl"\npath = "feed.jsonl"\n')
    with dakc.open(root) as kb:
        kb.compile()
        write_files(root / "knowledge", {"queues/kafka.md": "# Kafka\n\nKafka supports Topics.\n"})
        kb.compile()
        r = kb.rollback(1)
        assert r.publications[0]["ok"] and r.publications[0]["deleted"] >= 1
        assert kb.publish(dry_run=True)[0].pending == 0
