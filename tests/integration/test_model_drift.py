"""Model, prompt and compiler-logic drift must invalidate exactly the affected artifacts."""

import pytest

from dakc import versions
from dakc.compiler import Compiler
from dakc.providers import make_provider
from dakc.providers.deterministic import TemplatePageWriter, TemplateSummarizer
from dakc.provenance.compare import canonical_state, diff_states
from dakc.storage import Store
from dakc.transactions import rollback

from tests.conftest import POSTGRES_DOCS, write_files


class WriterV2(TemplatePageWriter):
    fingerprint = "template-writer/2"  # a new model or prompt

    def write(self, name, claims):
        r = super().write(name, claims)
        r.text = r.text.replace(" documented fact", " recorded fact")
        return r


class SummarizerV2(TemplateSummarizer):
    fingerprint = "template-summarizer/test-drift"


def bundle(writer=None, summarizer=None):
    b = make_provider("mock")
    if writer:
        b.writer = writer
    if summarizer:
        b.summarizer = summarizer
    return b


def run(store, src, b=None, **kw):
    rep, _ = Compiler(store, b or bundle(), src, **kw).run()
    return rep


def equals_full(store, src, tmp_path, b=None):
    ref = Store(tmp_path / "ref.db")
    run(ref, src, b, strategy="full")
    d = diff_states(canonical_state(ref), canonical_state(store))
    ref.close()
    return d.equal


@pytest.fixture
def built(kb):
    src, store = kb
    write_files(src, POSTGRES_DOCS)
    run(store, src)
    return src, store


def test_writer_change_regenerates_every_page_and_nothing_upstream(built, tmp_path):
    src, store = built
    n_pages = len(store.load_state().of("page"))
    b = bundle(writer=WriterV2(as_llm=True))
    rep = run(store, src, b)
    assert rep.n("regenerate_page") == n_pages
    assert rep.usage.by_stage.get("extract", 0) == 0
    assert all(p.generator == "template-writer/2" for p in store.load_state().of("page").values())
    assert run(store, src, b).status == "noop"
    assert equals_full(store, src, tmp_path, b)


def test_summarizer_change_regenerates_only_summaries(built, tmp_path):
    src, store = built
    b = bundle(summarizer=SummarizerV2(as_llm=True))
    rep = run(store, src, b)
    assert rep.n("regenerate_summary") == len(store.load_state().of("summary")) > 0
    assert rep.n("regenerate_page") == 0 and rep.usage.by_stage.get("extract", 0) == 0
    assert equals_full(store, src, tmp_path, b)


def test_render_logic_change_rerenders_without_provider_calls(built, tmp_path, monkeypatch):
    src, store = built
    monkeypatch.setattr(versions, "RENDER", "render/99")
    rep = run(store, src)
    assert rep.usage.calls == 0
    assert rep.n("rerender_page") == len(store.load_state().of("page"))
    assert rep.n("rerender_summary") > 0 and rep.n("rerender_index") == 1
    assert run(store, src).status == "noop"
    assert equals_full(store, src, tmp_path)


def test_parser_change_reparses_every_source(built, tmp_path, monkeypatch):
    src, store = built
    monkeypatch.setattr(versions, "PARSER", "parse/99")
    rep = run(store, src)
    assert rep.counts["sources_processed"] == 3
    assert rep.usage.calls == 0, "unchanged passages keep their ids, so nothing is re-extracted"
    assert rep.status == "committed" and store.load_state().compiler_versions["parser"] == "parse/99"
    assert run(store, src).status == "noop"


def test_claim_identity_change_rederives_claims_from_cache(built, tmp_path, monkeypatch):
    src, store = built
    n_passages = len(store.load_state().of("passage"))
    monkeypatch.setattr(versions, "CLAIM_IDENTITY", "claim-id/99")
    rep = run(store, src)
    assert rep.n("claim_identity_changed") == n_passages
    assert rep.usage.cache_hits == n_passages and rep.usage.calls == 0
    assert equals_full(store, src, tmp_path)


def test_store_without_recorded_versions_is_treated_as_outdated(built, tmp_path):
    src, store = built
    store.set_meta("compiler_versions", "{}")
    rep = run(store, src)
    assert rep.usage.by_stage.get("extract", 0) == 0, "re-derived from the cache"
    assert any("logic changed" in n for n in rep.notes)
    assert store.load_state().compiler_versions == versions.current()
    assert equals_full(store, src, tmp_path)


def test_rollback_restores_recorded_versions(built, monkeypatch):
    src, store = built
    head = store.head
    monkeypatch.setattr(versions, "RENDER", "render/99")
    run(store, src)
    assert store.load_state().compiler_versions["render"] == "render/99"
    rollback(store, head)
    assert store.load_state().compiler_versions["render"] == "render/1"
