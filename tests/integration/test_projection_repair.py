"""Projection repair: wiki files edited or deleted by hand are restored on every compile
(including a no-change build and a failed one), cheaply, and only files DAKC wrote are touched."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import dakc
from dakc.compiler import projection
from dakc.compiler.projection import MANIFEST, sync_projection

from tests.conftest import POSTGRES_DOCS, write_files

@pytest.fixture
def kb(tmp_path):
    write_files(tmp_path / "knowledge", POSTGRES_DOCS)
    assert dakc.ingest(tmp_path / "knowledge", tmp_path, provider="mock").status == "committed"
    with dakc.open(tmp_path) as kb:
        yield kb


def wiki_snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def count_reads(monkeypatch) -> list[Path]:
    """Record every wiki file the projection reads and hashes."""
    reads: list[Path] = []
    real = projection._text_hash

    def spy(path: Path) -> str | None:
        reads.append(path)
        return real(path)

    monkeypatch.setattr(projection, "_text_hash", spy)
    return reads


def age_manifest(kb) -> None:
    """Make every recorded stat older than the manifest, as it is on any file system with
    fine timestamps (the racy-clean rule is tested on its own below)."""
    m = kb.workspace.state_dir / MANIFEST
    later = max(p.stat().st_mtime_ns for p in kb.output_root.rglob("*.md")) + 2_000_000_000
    os.utime(m, ns=(later, later))


def test_hand_edit_is_restored_by_a_no_change_build(kb):
    page = kb.output_root / "pages" / "jsonb.md"
    original = page.read_bytes()
    page.write_text("# JSONB\n\nSomething an agent wrote by hand.\n", encoding="utf-8")
    report = kb.compile()
    assert report.status == "noop" and not report.has_work()
    assert page.read_bytes() == original
    p = report.projection
    assert p["repaired"] == 1 and p["repaired_paths"] == ["pages/jsonb.md"]
    assert p["written"] == 0 and p["deleted"] == 0
    assert p["written"] + p["repaired"] + p["unchanged"] == len(list(kb.output_root.rglob("*.md")))
    again = kb.compile().projection  # nothing left to repair
    assert again["repaired"] == 0 and again["repaired_paths"] == []


def test_deleted_file_is_restored(kb):
    before = wiki_snapshot(kb.output_root)
    (kb.output_root / "pages" / "redis.md").unlink()
    (kb.output_root / "index.md").unlink()
    p = kb.compile().projection
    assert p["repaired_paths"] == ["index.md", "pages/redis.md"] and p["repaired"] == 2
    assert wiki_snapshot(kb.output_root) == before


def test_committed_build_counts_repairs_apart_from_writes(kb):
    (kb.output_root / "pages" / "kafka.md").write_text("tampered\n", encoding="utf-8")
    write_files(kb.source_root, {"databases/redis.md": "# Redis\n\nRedis stores data in memory. Redis supports Clustering.\n"})
    report = kb.compile()
    assert report.status == "committed"
    p = report.projection
    assert p["repaired_paths"] == ["pages/kafka.md"] and p["written"] >= 1  # Kafka's page did not change: a repair
    assert p["written"] + p["repaired"] + p["unchanged"] == len(list(kb.output_root.rglob("*.md")))
    assert kb.verify().ok


def test_failed_build_keeps_head_and_repairs_the_wiki_to_it(kb):
    head = kb.head
    before = wiki_snapshot(kb.output_root)
    (kb.output_root / "pages" / "postgresql.md").write_text("hand edit\n", encoding="utf-8")
    (kb.output_root / "summaries" / "databases.md").unlink()
    write_files(kb.source_root, {"databases/new.md": "CockroachDB extends PostgreSQL.\n"})
    with pytest.raises(dakc.CompileFailed) as info:
        kb.compile(fault="commit:2")
    exc = info.value
    assert exc.preserved and exc.head == head == kb.head
    assert exc.projection["repaired_paths"] == ["pages/postgresql.md", "summaries/databases.md"]
    assert exc.projection["written"] == 0 and exc.projection["deleted"] == 0
    assert wiki_snapshot(kb.output_root) == before  # exactly HEAD: the failed build's pages are not projected
    assert not (kb.output_root / "pages" / "cockroachdb.md").exists()


def test_failed_build_reports_a_repair_that_could_not_run(kb, monkeypatch):
    def broken(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("dakc.kb.sync_projection", broken)
    write_files(kb.source_root, {"databases/new.md": "CockroachDB extends PostgreSQL.\n"})
    with pytest.raises(dakc.CompileFailed, match="InjectedFault") as info:  # the build's own error, not the repair's
        kb.compile(fault="commit:2")
    assert info.value.projection is None


def test_user_files_are_never_touched(kb):
    user = {
        "notes.md": "my own notes\n",
        "pages/my-page.md": "a page I wrote myself\n",
        "summaries/draft.txt": "draft\n",
        "assets/logo.svg": "<svg/>",
    }
    write_files(kb.output_root, user)
    (kb.output_root / "pages" / "jsonb.md").write_text("tampered\n", encoding="utf-8")  # one repair in the same run
    kb.compile()
    (kb.source_root / "messaging" / "kafka.md").unlink()  # a build that deletes projected files too
    report = kb.compile()
    assert report.projection["deleted"] >= 1 and not (kb.output_root / "pages" / "kafka.md").exists()
    for rel, text in user.items():
        assert (kb.output_root / rel).read_text(encoding="utf-8") == text, rel
    listed = json.loads((kb.workspace.state_dir / MANIFEST).read_text(encoding="utf-8"))
    assert not set(user) & set(listed["files"]) and not set(user) & set(listed["stat"])


def test_rollback_also_repairs(kb):
    write_files(kb.source_root, {"databases/new.md": "CockroachDB extends PostgreSQL.\n"})
    kb.compile()
    (kb.output_root / "pages" / "redis.md").write_text("tampered\n", encoding="utf-8")
    r = kb.rollback()
    assert r.projection["repaired_paths"] == ["pages/redis.md"] and r.projection["deleted"] >= 1
    assert kb.verify().ok


# ------------------------------------------------------------------ cost
def test_unchanged_files_are_not_read(kb, monkeypatch):
    age_manifest(kb)
    reads = count_reads(monkeypatch)
    p = kb.compile().projection
    assert reads == [] and p["unchanged"] == len(list(kb.output_root.rglob("*.md")))


def test_a_file_is_read_only_when_its_size_or_mtime_changed(kb, monkeypatch):
    age_manifest(kb)
    touched = kb.output_root / "pages" / "redis.md"
    os.utime(touched, ns=(touched.stat().st_atime_ns, touched.stat().st_mtime_ns + 5_000_000_000))  # same bytes, new mtime
    edited = kb.output_root / "pages" / "kafka.md"
    text = edited.read_text(encoding="utf-8")
    edited.write_text(text.replace("Kafka", "Kafkb", 1), encoding="utf-8")  # same size, new mtime
    reads = count_reads(monkeypatch)
    p = sync_projection(kb.store, kb.output_root, kb.workspace.state_dir)
    assert sorted(x.name for x in reads) == ["kafka.md", "redis.md"]
    assert p["repaired_paths"] == ["pages/kafka.md"] and edited.read_text(encoding="utf-8") == text
    age_manifest(kb)
    reads.clear()
    sync_projection(kb.store, kb.output_root, kb.workspace.state_dir)
    assert reads == []  # both stats were recorded: the touched file is trusted again


def test_racily_clean_files_are_hashed(kb, monkeypatch):
    """On a file system with coarse timestamps, an edit in the same tick as DAKC's write keeps
    size and mtime. A recorded stat that is not older than the manifest is therefore not trusted."""
    page = kb.output_root / "pages" / "jsonb.md"
    st = page.stat()
    text = page.read_text(encoding="utf-8")
    page.write_text(text.replace("JSONB", "JSONC", 1), encoding="utf-8")
    os.utime(page, ns=(st.st_atime_ns, st.st_mtime_ns))  # size and mtime exactly as recorded
    m = kb.workspace.state_dir / MANIFEST
    os.utime(m, ns=(st.st_mtime_ns, st.st_mtime_ns))  # ... and written in the same tick as the manifest
    reads = count_reads(monkeypatch)
    p = sync_projection(kb.store, kb.output_root, kb.workspace.state_dir)
    assert p["repaired_paths"] == ["pages/jsonb.md"] and page.read_text(encoding="utf-8") == text
    assert page in reads


def test_manifest_without_stats_is_upgraded_without_rewriting(kb, monkeypatch):
    m = kb.workspace.state_dir / MANIFEST
    data = json.loads(m.read_text(encoding="utf-8"))
    m.write_text(json.dumps({"head": data["head"], "files": data["files"]}), encoding="utf-8")  # as written by 0.7
    before = {p: p.stat().st_mtime_ns for p in kb.output_root.rglob("*.md")}
    reads = count_reads(monkeypatch)
    p = sync_projection(kb.store, kb.output_root, kb.workspace.state_dir)
    assert len(reads) == len(before) and p["unchanged"] == len(before) and p["written"] == p["repaired"] == 0
    assert {q: q.stat().st_mtime_ns for q in kb.output_root.rglob("*.md")} == before
    assert set(json.loads(m.read_text(encoding="utf-8"))["stat"]) == {q.relative_to(kb.output_root).as_posix() for q in before}


def test_manifest_entry_without_text_hash_is_checked_against_the_store(kb):
    m = kb.workspace.state_dir / MANIFEST
    data = json.loads(m.read_text(encoding="utf-8"))
    data["files"].pop("pages/redis.md#text")
    data["files"].pop("pages/kafka.md#text")
    data["stat"] = {}
    m.write_text(json.dumps(data), encoding="utf-8")
    (kb.output_root / "pages" / "kafka.md").write_text("tampered\n", encoding="utf-8")
    p = sync_projection(kb.store, kb.output_root, kb.workspace.state_dir)
    assert p["repaired_paths"] == ["pages/kafka.md"]
    files = json.loads(m.read_text(encoding="utf-8"))["files"]
    assert "pages/redis.md#text" in files and "pages/kafka.md#text" in files


def test_unreadable_text_is_repaired(kb):
    page = kb.output_root / "pages" / "indexing.md"
    original = page.read_bytes()
    page.write_bytes(b"\xff\xfe not utf-8")
    assert kb.compile().projection["repaired_paths"] == ["pages/indexing.md"]
    assert page.read_bytes() == original


# ---------------------------------------------------------- verify and surfaces
def test_verify_reports_drift_without_repairing(kb):
    page = kb.output_root / "pages" / "jsonb.md"
    page.write_text("tampered\n", encoding="utf-8")
    (kb.output_root / "pages" / "redis.md").unlink()
    v = kb.verify()
    assert not v.ok and not v.checks["wiki files match the store"]
    assert sorted(x.split(" ", 1)[0] for x in v.problems["wiki files match the store"]) == ["differs", "missing"]
    assert v.metrics["wiki_files_drifted"] == ["pages/jsonb.md", "pages/redis.md"]
    assert page.read_text(encoding="utf-8") == "tampered\n" and not (kb.output_root / "pages" / "redis.md").exists()
    kb.compile()
    assert kb.verify().ok and "wiki_files_drifted" not in kb.verify().metrics


def test_report_dict_caps_the_path_list_but_not_the_count(kb):
    for name in ("jsonb", "redis", "kafka"):
        (kb.output_root / "pages" / f"{name}.md").write_text("tampered\n", encoding="utf-8")
    report = kb.compile()
    d = report.to_dict(limit=2)["projection"]
    assert d["repaired"] == 3 and d["repaired_paths"] == ["pages/jsonb.md", "pages/kafka.md"]
    assert report.to_dict()["projection"]["repaired_paths"] == ["pages/jsonb.md", "pages/kafka.md", "pages/redis.md"]
