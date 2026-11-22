"""Hub pages (more than PAGE_FLAT claims): written in sections, citations stay valid."""

from __future__ import annotations

from pathlib import Path

import pytest

from dakc.compiler import PAGE_FLAT, SECTION_MAX, Compiler
from dakc.providers import make_provider
from dakc.provenance.trace import trace_page
from dakc.provenance.verify import verify_against_full, verify_store
from dakc.storage import Store

from tests.conftest import write_files


def hub_docs(n: int = 60, extra: str = "") -> dict[str, str]:
    guide = "# Guide\n\n## Setup\n\n" + "\n\n".join(f"Hub supports Feature{i:03d}." for i in range(n)) + extra + "\n"
    notes = "# Notes\n\n" + "\n\n".join(f"Hub works with Tool{i:03d}." for i in range(n)) + "\n"
    return {"guide/a.md": guide, "notes/b.md": notes}


class RecordingWriter:
    def __init__(self, inner):
        self.inner, self.sizes = inner, []
        self.fingerprint, self.free, self.is_llm = inner.fingerprint, inner.free, inner.is_llm

    def write(self, name, claims):
        if name == "Hub":
            self.sizes.append(len(claims))
        return self.inner.write(name, claims)


def compile_with(store, src):
    bundle = make_provider("mock")
    rec = RecordingWriter(bundle.writer)
    bundle.writer = rec
    report, _ = Compiler(store, bundle, src, jobs=1).run()
    return report, rec


def check(store: Store, src: Path) -> None:
    res = verify_store(store)
    assert res.ok, res.problems
    page = store.load_state().of("page")["page:hub"]
    t = trace_page(store, page)
    assert not t.invalid_markers and len(t.claims) == len(page.claim_ids)
    equal, detail = verify_against_full(store, src, make_provider("mock"), {})
    assert equal, detail


@pytest.fixture
def built(tmp_path):
    src = tmp_path / "knowledge"
    write_files(src, hub_docs())
    store = Store(tmp_path / ".dakc" / "dakc.db")
    report, rec = compile_with(store, src)
    yield src, store, report, rec
    store.close()


def test_a_hub_page_is_written_in_sections(built):
    src, store, report, rec = built
    page = store.load_state().of("page")["page:hub"]
    assert len(page.claim_ids) == 120 > PAGE_FLAT and len(page.sections) >= 2
    assert max(rec.sizes) <= SECTION_MAX and sum(rec.sizes) == 120  # every claim is written exactly once
    labels = [s[1] for s in page.sections]
    assert labels[0].startswith("Facts 1–") and labels[-1].split(",")[0].endswith("–120")  # page-wide numbers
    assert any("guide/a.md" in label for label in labels) and any("notes/b.md" in label for label in labels)
    assert page.lead.startswith("Hub has 120 documented facts")
    assert page.rendered.count("[^C120]") >= 1  # page-wide citation numbers
    check(store, src)


def test_editing_one_fact_rewrites_only_its_section(built):
    src, store, _, _ = built
    n_sections = len(store.load_state().of("page")["page:hub"].sections)
    docs = hub_docs()
    docs["notes/b.md"] = docs["notes/b.md"].replace("Hub works with Tool007.", "Hub integrates with Tool007.")
    write_files(src, docs)
    report, rec = compile_with(store, src)
    # The fact names Hub and Tool007, so both pages change. An edited fact is a retracted
    # claim plus a new one (new id), which may sort into another section: Hub rewrites the
    # section it left and the one it joined (one more only if a boundary moved), never all.
    assert sorted(report.ops["regenerate_page"]) == ["page:hub", "page:tool007"]
    assert 1 <= len(rec.sizes) <= 3 and max(rec.sizes) <= SECTION_MAX
    assert report.n("cutoff_section") >= n_sections - 3 and n_sections >= 5
    check(store, src)


def test_a_heading_inserted_above_unchanged_facts_updates_their_passages_and_rewrites_nothing(built):
    # Regression: a passage's id covers its text, not its heading. Inserting a heading kept
    # the passages and their claims, but also their old heading, while a full build used
    # the new one (trace named the wrong section, and full-rebuild equality failed).
    src, store, _, _ = built
    docs = hub_docs()
    docs["guide/a.md"] = docs["guide/a.md"].replace("Hub supports Feature030.", "### Later\n\nHub supports Feature030.")
    write_files(src, docs)
    report, rec = compile_with(store, src)
    assert report.n("passage_heading_changed") == 30 and report.counts["passages_added"] == 0  # nothing re-extracted
    assert report.n("regenerate_section") == 0 and report.n("regenerate_page") == 0 and rec.sizes == []
    page = store.load_state().of("page")["page:hub"]
    headings = {e.heading for c in trace_page(store, page).claims for e in c.evidence if "Feature030" in c.text}
    assert headings == {"Guide > Setup > Later"}
    check(store, src)


def test_moved_citations_re_render_a_hub_without_rewriting_any_section(tmp_path):
    # The guarantee every page has: claims unchanged, citations moved -> re-render, no LLM call.
    src = tmp_path / "knowledge"
    write_files(src, {**hub_docs(), "extra/c.md": "# Extra\n\nHub supports Feature010.\n"})
    store = Store(tmp_path / ".dakc" / "dakc.db")
    compile_with(store, src)
    (src / "extra" / "c.md").unlink()  # the fact is still stated in guide/a.md
    report, rec = compile_with(store, src)
    assert report.n("claim_provenance_changed") == 1 and report.n("new_claim") == 0
    assert report.n("regenerate_section") == 0 and report.n("regenerate_page") == 0 and rec.sizes == []
    assert "page:hub" in report.ops["rerender_page"]
    check(store, src)
    store.close()


def test_upgrading_repairs_headings_left_stale_by_older_versions(tmp_path, monkeypatch):
    from dakc import versions

    src = tmp_path / "knowledge"
    write_files(src, hub_docs())
    store = Store(tmp_path / ".dakc" / "dakc.db")
    monkeypatch.setattr(versions, "PARSER", "parse/2")
    compile_with(store, src)
    monkeypatch.undo()
    # what parse/2 left behind after a heading was inserted above unchanged passages
    store.conn.execute("UPDATE artifacts SET payload = json_set(payload, '$.heading', 'Guide > Old') "
                       "WHERE kind = 'passage' AND to_build IS NULL AND payload LIKE '%Feature0%'")
    store.conn.commit()
    report, _ = compile_with(store, src)
    assert any("parser" in n for n in report.notes) and report.n("passage_heading_changed") == 60
    assert report.counts["passages_added"] == 0  # repaired without re-extracting anything
    assert {p.heading for p in store.load_state().of("passage").values() if "Feature0" in p.text} == {"Guide > Setup"}
    check(store, src)
    store.close()


def test_an_insertion_renumbers_citations_without_rewriting_other_sections(built):
    src, store, _, _ = built
    before = store.load_state().of("page")["page:hub"]
    write_files(src, hub_docs(extra="\n\nHub supports Feature0255b."))  # lands mid-guide, shifts later numbers
    report, rec = compile_with(store, src)
    after = store.load_state().of("page")["page:hub"]
    assert len(after.claim_ids) == 121 and len(rec.sizes) <= 2  # its own section (twice if it split)
    unchanged = {s[0] for s in before.sections} & {s[0] for s in after.sections}
    assert len(unchanged) >= len(before.sections) - 1
    check(store, src)
