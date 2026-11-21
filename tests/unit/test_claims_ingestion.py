from dakc.claims import extract_mentions, heuristic_extract, match_revisions, numbers_differ, segment
from dakc.ingestion import SourceRecord, new_source_id, scan
from dakc.parsing import parse_bytes

from tests.conftest import write_files


def test_mentions_group_capitalized_runs_and_skip_stopwords():
    assert extract_mentions("The Amazon Web Services console talks to PostgreSQL.") == ("Amazon Web Services", "PostgreSQL")
    assert extract_mentions("However, it is fast.") == ()
    assert extract_mentions("We use iPhone and JSONB daily.") == ("JSONB", "iPhone")


def test_heuristic_extract_one_claim_per_entity_sentence():
    claims = heuristic_extract("PostgreSQL supports JSONB. It is great. Redis stores data in memory!")
    assert [c.text for c in claims] == ["PostgreSQL supports JSONB.", "Redis stores data in memory!"]
    assert claims[0].mentions == ("JSONB", "PostgreSQL")


def test_segment_ids_are_content_addressed_with_occurrence():
    doc = parse_bytes(b"# T\n\nSame text.\n\nSame text.\n\nOther text.\n", "markdown")
    ps = segment("src:a", doc)
    assert [p.occurrence for p in ps] == [0, 1, 0]
    assert len({p.id for p in ps}) == 3
    moved = segment("src:a", parse_bytes(b"# T\n\nOther text.\n\nSame text.\n\nSame text.\n", "markdown"))
    assert {p.id for p in moved} == {p.id for p in ps}, "reordering paragraphs must not change passage ids"


def test_revision_matching_is_for_history_only():
    pairs = match_revisions({"a": "PostgreSQL 15 supports JSONB."}, {"b": "PostgreSQL 16 supports JSONB.", "c": "Redis is fast."}, threshold=80)
    assert pairs and pairs[0][:2] == ("a", "b")
    assert numbers_differ("PostgreSQL 15 supports JSONB.", "PostgreSQL 16 supports JSONB.")


def _registry(store_like):
    return {r.id: r for r in store_like}


def test_scan_classifies_new_modified_deleted_renamed_unchanged(tmp_path):
    root = tmp_path / "k"
    write_files(root, {"a.md": "A", "b.md": "B", "c.md": "C", "d.md": "D", "img.png": "x", ".hidden/e.md": "E"})
    first = scan(root, {})
    assert sorted(first.new) == ["a.md", "b.md", "c.md", "d.md"]
    taken: set[str] = set()
    reg = {}
    for path in first.new:
        sid = new_source_id(path, taken)
        info = first.files[path]
        reg[sid] = SourceRecord(sid, path, info.raw_hash, "", info.size, info.source_type)
    (root / "a.md").write_text("A changed")
    (root / "b.md").rename(root / "b2.md")
    (root / "c.md").unlink()
    (root / "f.md").write_text("D")  # duplicate content of d.md
    sr = scan(root, reg)
    assert [p for _, p in sr.modified] == ["a.md"]
    assert [(o, n) for _, o, n in sr.renamed] == [("b.md", "b2.md")]
    assert sr.deleted == ["src:c-md"]
    assert sr.new == ["f.md"]
    assert sr.unchanged == ["src:d-md"]
    assert sr.duplicates == [["d.md", "f.md"]]
    assert scan(root, reg, detect_renames=False).new == ["b2.md", "f.md"]


def test_source_ids_avoid_collisions():
    taken = {"src:a-md"}
    assert new_source_id("a.md", taken) == "src:a-md-2"
    assert new_source_id("a.md", taken) == "src:a-md-3"
