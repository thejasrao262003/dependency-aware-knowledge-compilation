"""Sources (RFC 0001): several connectors in one workspace, identity, failures, plugins."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import dakc
from dakc.compiler import Compiler
from dakc.errors import CompileFailed, SourceError
from dakc.providers import make_provider
from dakc.provenance.verify import verify_against_full
from dakc.sources import NATIVE_IDS, FilesystemSource, Item, Source, SourceSet, make_source
from dakc.storage import Store
from dakc.transactions import state_digest

from tests.conftest import write_files


class MemorySource:
    """Behaves like a cloud drive: native ids, opaque revisions, no content hashes."""

    kind = "memory"
    capabilities = frozenset({NATIVE_IDS})

    def __init__(self, docs: dict[str, tuple[str, str, str]] | None = None):
        self.docs = dict(docs or {})  # key -> (path, text, revision)
        self.reads: list[str] = []
        self.fail = False

    def list(self):
        if self.fail:
            raise ConnectionError("service unavailable")
        for key, (path, text, rev) in sorted(self.docs.items()):
            yield Item(path, "markdown", rev, len(text), key=key)

    def read(self, item: Item) -> bytes:
        self.reads.append(item.key)
        path, text, rev = self.docs[item.key]
        if rev != item.revision:
            raise RuntimeError("revision moved on")
        return text.encode()


def build(store: Store, ss: SourceSet, **kw):
    report, _ = Compiler(store, make_provider("mock"), ss, jobs=1, **kw).run()
    return report


def assert_equal_to_full(store: Store, ss: SourceSet) -> None:
    equal, detail = verify_against_full(store, ss, make_provider("mock"), {})
    assert equal, detail


def claim_sources(store: Store, needle: str) -> tuple[str, set[str]]:
    st = store.load_state()
    claim = next(c for c in st.of("claim").values() if needle in c.text)
    return claim.id, {st.sources[st.of("passage")[p].source_id].path for p in claim.passage_ids}


@pytest.fixture
def two(tmp_path):
    docs = tmp_path / "docs"
    write_files(docs, {"db.md": "# DB\n\nPostgreSQL supports JSONB.\n", "cache.md": "# Cache\n\nRedis supports Streams.\n"})
    mem = MemorySource({"k1": ("notes/db.md", "# Notes\n\nPostgreSQL supports JSONB.\n", "r1")})
    ss = SourceSet([Source("docs", FilesystemSource(docs)), Source("drive", mem, "drive")])
    store = Store(tmp_path / ".dakc" / "dakc.db")
    build(store, ss)
    yield docs, mem, ss, store
    store.close()


def test_a_fact_stated_in_two_sources_survives_deleting_either(two):
    docs, mem, ss, store = two
    cid, paths = claim_sources(store, "JSONB")
    assert paths == {"db.md", "drive/notes/db.md"}
    assert_equal_to_full(store, ss)
    del mem.docs["k1"]
    report = build(store, ss)
    assert report.n("source_deleted") == 1 and report.n("retract_claim") == 0
    assert claim_sources(store, "JSONB") == (cid, {"db.md"})
    assert_equal_to_full(store, ss)
    (docs / "db.md").unlink()
    build(store, ss)
    assert cid not in store.load_state().of("claim")  # no source states it any more
    assert_equal_to_full(store, ss)


def test_a_moved_and_edited_document_keeps_its_identity_with_native_ids(two):
    docs, mem, ss, store = two
    sid = next(s.id for s in store.load_sources().values() if s.path == "drive/notes/db.md")
    mem.docs["k1"] = ("archive/db.md", "# Notes\n\nPostgreSQL supports JSONB. PostgreSQL supports Indexing.\n", "r2")
    report = build(store, ss)
    assert report.ops["source_renamed"] == [sid] and report.ops["source_modified"] == [sid]
    assert store.current_source(sid).path == "drive/archive/db.md" and store.current_source(sid).key == "k1"
    assert_equal_to_full(store, ss)


def test_an_unchanged_revision_is_never_read(two):
    docs, mem, ss, store = two
    mem.reads.clear()
    assert build(store, ss).status == "noop" and mem.reads == []
    path, text, _ = mem.docs["k1"]
    mem.docs["k1"] = (path, text, "r1-touched")  # new revision, same bytes (a metadata edit)
    report = build(store, ss)
    assert mem.reads == ["k1"] and report.n("source_revised") == 1 and report.n("source_modified") == 0
    assert report.counts["passages_added"] == 0
    mem.reads.clear()
    assert build(store, ss).status == "noop" and mem.reads == []  # the new revision was recorded


def test_the_same_document_in_two_sources_has_two_identities(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write_files(a, {"x.md": "# X\n\nKafka supports Topics.\n"})
    write_files(b, {"y.md": "# Y\n\nNATS supports Subjects.\n"})
    ss = SourceSet([Source("a", FilesystemSource(a)), Source("b", FilesystemSource(b), "b")])
    store = Store(tmp_path / "s.db")
    build(store, ss)
    # moving a file from one source to another is a deletion and a new source, never a rename
    shutil.move(a / "x.md", b / "x.md")
    report = build(store, ss)
    assert report.n("source_renamed") == 0 and report.n("source_deleted") == 1 and report.n("source_new") == 1
    assert_equal_to_full(store, ss)
    write_files(a, {"x.md": "# X\n\nKafka supports Topics.\n"})  # now in both
    report = build(store, ss)
    assert report.n("source_new") == 1 and report.scan["duplicates"] == [["b/x.md", "x.md"]]
    assert_equal_to_full(store, ss)
    store.close()


def test_a_prefix_that_shadows_a_directory_is_refused(tmp_path):
    docs = tmp_path / "docs"
    write_files(docs, {"drive/readme.md": "# R\n\nRedis supports Streams.\n"})
    ss = SourceSet([Source("docs", FilesystemSource(docs)), Source("drive", MemorySource(), "drive")])
    store = Store(tmp_path / "s.db")
    with pytest.raises(SourceError, match="inside the prefix"):
        build(store, ss)
    assert store.head == 0
    store.close()


@pytest.mark.parametrize("spec, error", [
    ([("a", ""), ("b", "")], "empty prefix"),
    ([("a", "x"), ("b", "x")], "prefixes must be unique"),
    ([("a", "x/y")], "single path segment"),
    ([("a", ""), ("a", "b")], "names must be unique"),
])
def test_invalid_source_sets_are_refused(tmp_path, spec, error):
    with pytest.raises(SourceError, match=error):
        SourceSet([Source(n, FilesystemSource(tmp_path), p) for n, p in spec])


def test_a_source_that_cannot_be_listed_fails_the_build_and_changes_nothing(two):
    docs, mem, ss, store = two
    head, digest = store.head, state_digest(store)
    mem.fail = True
    with pytest.raises(SourceError, match="'drive'.*could not be listed.*service unavailable"):
        build(store, ss)
    assert store.head == head and state_digest(store) == digest
    mem.fail = False
    assert build(store, ss).status == "noop"


def test_a_missing_directory_with_registered_sources_is_refused(two):
    docs, mem, ss, store = two
    head = store.head
    shutil.rmtree(docs)
    with pytest.raises(SourceError, match="directory .* is missing"):
        build(store, ss)
    assert store.head == head
    docs.mkdir()  # an explicit empty directory deletes them
    assert build(store, ss).n("source_deleted") == 2


def test_a_new_workspace_may_start_without_its_directory(tmp_path):
    store = Store(tmp_path / "s.db")
    report = build(store, SourceSet.from_root(tmp_path / "not-yet"))  # an empty source, not an error
    assert report.status == "committed" and store.load_sources() == {}
    store.close()


def test_a_store_from_before_sources_upgrades_without_rebuilding(tmp_path):
    src = tmp_path / "knowledge"
    write_files(src, {"db.md": "# DB\n\nPostgreSQL supports JSONB.\n"})
    store = Store(tmp_path / "s.db")
    build(store, SourceSet.from_root(src))
    for col in ("key", "revision"):  # what a 0.4 store looks like
        store.conn.execute(f"ALTER TABLE sources DROP COLUMN {col}")
    store.set_meta("schema_version", "1")
    store.close()
    store = Store(tmp_path / "s.db")
    assert store.get_meta("schema_version") == "2"
    assert build(store, SourceSet.from_root(src)).status == "noop"
    store.close()


# ------------------------------------------------------------- workspace / plugins
PLUGIN = '''
import json
from pathlib import Path
from dakc.sources import Item

class JsonDocs:
    """Documents kept in one JSON file: {key: {"path", "text", "rev"}}."""
    kind = "jsondocs"
    capabilities = frozenset({"native_ids"})

    def __init__(self, path):
        self.path = Path(path)

    def _data(self):
        return json.loads(self.path.read_text())

    def list(self):
        for key, d in sorted(self._data().items()):
            yield Item(d["path"], "markdown", d["rev"], len(d["text"]), key=key)

    def read(self, item):
        return self._data()[item.key]["text"].encode()

def make(name, options, root):
    return JsonDocs(root / options["path"])
'''


def workspace_with_two_sources(root: Path) -> Path:
    write_files(root / "knowledge", {"db.md": "# DB\n\nPostgreSQL supports JSONB.\n"})
    write_files(root / "plugins", {"jsondocs.py": PLUGIN})
    (root / "crm.json").write_text(json.dumps({"a1": {"path": "accounts/acme.md", "rev": "1",
                                                      "text": "# Acme\n\nAcme uses PostgreSQL.\n"}}))
    (root / "dakc.toml").write_text(
        '[dakc]\nsource_dir = "knowledge"\nprovider = "mock"\n\n'
        '[[sources]]\nname = "docs"\ntype = "filesystem"\npath = "knowledge"\nprefix = ""\n\n'
        '[[sources]]\nname = "crm"\ntype = "custom"\nfactory = "jsondocs:make"\nplugin_path = "plugins"\npath = "crm.json"\n',
        encoding="utf-8",
    )
    return root


def test_a_workspace_with_a_local_connector_plugin(tmp_path):
    root = workspace_with_two_sources(tmp_path)
    with dakc.open(root) as kb:
        assert kb.compile().status == "committed"
        infos = {i.name: i for i in kb.sources()}
        assert infos["docs"].documents == 1 and infos["crm"].documents == 1
        assert infos["crm"].type == "jsondocs" and infos["crm"].prefix == "crm" and infos["crm"].capabilities == ["native_ids"]
        assert kb.status().up_to_date and kb.check().unchanged == 2
        assert "Acme uses PostgreSQL" in kb.read("crm/accounts/acme.md").text
        assert kb.verify(against_full=True).ok
        data = json.loads((root / "crm.json").read_text())
        data["a1"].update(path="customers/acme.md", rev="2")  # moved in the CRM
        (root / "crm.json").write_text(json.dumps(data))
        assert kb.check().renamed == [("crm/accounts/acme.md", "crm/customers/acme.md")]
        report = kb.compile()
        assert report.n("source_renamed") == 1 and report.counts["passages_added"] == 0
        assert kb.verify(against_full=True).ok


def test_installed_connectors_are_found_by_entry_point(tmp_path, monkeypatch):
    import dakc.sources as sources

    class EP:
        def load(self):
            return lambda name, options, root: MemorySource()

    monkeypatch.setattr(sources, "_entry_points", lambda group: {"memory": EP()} if group == sources.SOURCE_GROUP else {})
    src = make_source({"name": "drive", "type": "memory"}, tmp_path)
    assert src.connector.kind == "memory" and src.prefix == "drive"
    with pytest.raises(SourceError, match=r"unknown source type 'gdrive'; available: custom, filesystem, memory"):
        make_source({"name": "x", "type": "gdrive"}, tmp_path)


def test_a_connector_error_reaches_the_api_as_a_failed_compile(tmp_path):
    root = workspace_with_two_sources(tmp_path)
    with dakc.open(root) as kb:
        kb.compile()
        (root / "crm.json").write_text("not json")
        with pytest.raises(CompileFailed, match="SourceError: source 'crm'") as exc:
            kb.compile()
        assert exc.value.preserved
