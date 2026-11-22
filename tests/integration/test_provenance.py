"""Provenance: generated sentence -> claim -> passage -> source version -> evidence sentence."""

from dakc.compiler import Compiler
from dakc.providers import make_provider
from dakc.providers.base import GenResult
from dakc.providers.deterministic import TemplatePageWriter
from dakc.provenance.trace import citation_coverage, trace_page
from dakc.provenance.verify import verify_store

from tests.conftest import compile_once, write_files

DOCS = {
    "db/postgres.md": "# PostgreSQL\n\nPostgreSQL is the primary Database. It supports JSONB. Patroni manages PostgreSQL.\n",
    "db/notes.txt": "Patroni manages PostgreSQL.\n\nEtcd stores the Patroni state.\n",
}


def page(store, pid):
    return store.current_artifact(pid)[0]


def test_every_fact_sentence_traces_to_exact_source_sentences(kb):
    src, store = kb
    write_files(src, DOCS)
    compile_once(store, src)
    t = trace_page(store, page(store, "page:patroni"))
    facts = [s for s in t.sentences if s.cites]
    assert facts and not t.invalid_markers
    claims = {c.ref: c for c in t.claims}
    for s in facts:
        for ref in s.cites:
            for ev in claims[ref].evidence:
                assert ev.source_path in ("db/postgres.md", "db/notes.txt")
                assert ev.sentences and all(x in (src / ev.source_path).read_text() for x in ev.sentences)
                assert len(ev.source_raw_hash) == 16 and ev.source_version_build == 1
    shared = next(c for c in t.claims if c.text == "Patroni manages PostgreSQL.")
    assert {e.source_path for e in shared.evidence} == {"db/postgres.md", "db/notes.txt"}, "both supports are traced"


def test_trace_follows_source_versions(kb):
    src, store = kb
    write_files(src, DOCS)
    compile_once(store, src)
    write_files(src, {"db/notes.txt": "Patroni manages PostgreSQL.\n\nEtcd stores the Patroni leader key.\n"})
    rep = compile_once(store, src)
    t = trace_page(store, page(store, "page:etcd"))
    ev = t.claims[0].evidence[0]
    assert ev.source_version_build == rep.build_id and ev.sentences == ["Etcd stores the Patroni leader key."]


class BadCitations(TemplatePageWriter):
    fingerprint = "bad-citations/1"

    def write(self, name, claims):
        return GenResult(f"{name} is important [^C99].\n\nIt is used widely.", 1, 1)


def test_verify_rejects_citations_to_nonexistent_claims(kb):
    src, store = kb
    write_files(src, DOCS)
    b = make_provider("mock")
    b.writer = BadCitations(as_llm=True)
    Compiler(store, b, src).run()
    res = verify_store(store)
    assert not res.checks["page citation markers point to the page's own claims"]
    cov = citation_coverage(store)
    assert cov["coverage"] == 0.0 and cov["invalid_markers"] > 0


def test_verify_reports_citation_coverage_for_template_pages(kb):
    src, store = kb
    write_files(src, DOCS)
    compile_once(store, src)
    res = verify_store(store)
    assert res.ok
    cov = res.metrics["citation_coverage"]
    # template pages: one statistical lead sentence per page is uncited, every fact line is cited
    n_pages = len(store.load_state().of("page"))
    assert cov["sentences"] - cov["cited"] == n_pages


class Embellishing(TemplatePageWriter):
    """Imitates an LLM page writer that adds an unsupported sentence."""

    fingerprint = "embellishing/1"

    def write(self, name, claims):
        r = super().write(name, claims)
        r.text += f"\n\n{name} is widely regarded as the best choice."
        return r


def test_strict_provenance_keeps_only_cited_sentences_without_llm_calls(kb, tmp_path):
    src, store = kb
    write_files(src, DOCS)
    b = make_provider("mock")
    b.writer = Embellishing(as_llm=True)
    Compiler(store, b, src).run()
    p = page(store, "page:patroni")
    assert "widely regarded" in p.rendered and not p.dropped
    rep, _ = Compiler(store, b, src, provenance="strict").run()
    assert "page" not in rep.usage.by_stage and "extract" not in rep.usage.by_stage, "pages are only re-rendered"
    # (a summary may regenerate: dropping the uncited lead legitimately changes the page's lead paragraph)
    p = page(store, "page:patroni")
    assert "widely regarded" not in p.rendered and any("widely regarded" in d for d in p.dropped)
    assert "widely regarded" in p.body, "the generated text is kept for audit"
    assert citation_coverage(store)["coverage"] == 1.0
    ref_store = type(store)(tmp_path / "ref.db")
    Compiler(ref_store, b, src, strategy="full", provenance="strict").run()
    from dakc.provenance.compare import canonical_state, diff_states
    assert diff_states(canonical_state(ref_store), canonical_state(store)).equal
