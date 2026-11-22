"""Hierarchical topic summaries: bounded prompts, local regeneration, equal to a full rebuild."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from dakc import versions
from dakc.compiler import SECTION_FLAT, SECTION_MAX, Compiler, content_sections
from dakc.providers import make_provider
from dakc.provenance.verify import verify_against_full
from dakc.storage import Store

from tests.conftest import write_files


def big_topic(root: Path, n: int = 80) -> None:
    """One folder whose documents mention 2n distinct entities: a topic of about 2n pages."""
    write_files(root, {f"notes/doc{i:03d}.md": f"# Doc {i}\n\nAlpha{i:03d} is fast. Beta{i:03d} is reliable.\n" for i in range(n)})


class RecordingSummarizer:
    """Wraps a summarizer and records how many items each call receives."""

    def __init__(self, inner):
        self.inner, self.sizes = inner, []
        self.fingerprint, self.free, self.is_llm = inner.fingerprint, inner.free, inner.is_llm

    def summarize(self, topic, pages):
        self.sizes.append(len(pages))
        return self.inner.summarize(topic, pages)


def compile_with(store: Store, src: Path, **kw):
    bundle = make_provider("mock")
    rec = RecordingSummarizer(bundle.summarizer)
    bundle.summarizer = rec
    report, _ = Compiler(store, bundle, src, jobs=1, **kw).run()
    # every summary is reported once: regenerated, re-rendered, checked and unchanged, or deleted
    ops = [report.ops.get(k, []) for k in ("regenerate_summary", "rerender_summary", "cutoff_summary", "delete_summary")]
    assert sum(len(o) for o in ops) == len(set().union(*ops))
    return report, rec


# ---------------------------------------------------------------- the split
def test_sections_are_a_pure_function_of_the_keys_and_bounded():
    keys = sorted(f"page:k{i}" for i in range(3000))
    a = content_sections(keys, key=lambda k: k, fanin=8)
    assert a == content_sections(list(keys), key=lambda k: k, fanin=8)  # deterministic
    assert [k for s in a for k in s] == keys and max(len(s) for s in a) <= 32
    assert 3000 / len(a) == pytest.approx(8, rel=0.25)  # expected size close to the fan-in


def test_an_insertion_disturbs_only_its_own_section():
    rng = random.Random(7)
    keys = sorted(f"page:k{rng.random()}" for _ in range(2000))
    before = {tuple(s) for s in content_sections(keys, key=lambda k: k, fanin=16)}
    for _ in range(300):
        after = {tuple(s) for s in content_sections(sorted(keys + [f"page:new{rng.random()}"]), key=lambda k: k, fanin=16)}
        assert len(before - after) <= 1  # at most the section it landed in is replaced (by one, or two if it splits)


# ---------------------------------------------------------------- compile
@pytest.fixture
def built(tmp_path):
    src = tmp_path / "knowledge"
    big_topic(src)
    store = Store(tmp_path / ".dakc" / "dakc.db")
    report, rec = compile_with(store, src)
    yield src, store, report, rec
    store.close()


def test_large_topic_is_summarized_in_bounded_sections(built):
    src, store, report, rec = built
    summary = store.load_state().of("summary")["sum:notes"]
    assert len(summary.page_ids) > SECTION_FLAT and len(summary.sections) >= 3
    assert "## Sections" in summary.rendered and summary.rendered.count("### ") == len(summary.sections)
    assert max(rec.sizes) <= SECTION_MAX  # no prompt sees the whole topic
    assert report.n("regenerate_section") == len(summary.sections) and report.n("regenerate_summary") == 1


def test_an_edit_regenerates_only_its_section_and_the_topic(built, tmp_path):
    src, store, _, _ = built
    n_sections = len(store.load_state().of("summary")["sum:notes"].sections)
    # a page whose text changes but whose lead does not: the summary is cut off entirely
    write_files(src, {"notes/doc007.md": "# Doc 7\n\nAlpha007 is very fast. Beta007 is reliable.\n"})
    report, rec = compile_with(store, src)
    assert report.n("regenerate_page") == 1 and report.n("regenerate_section") == 0 and rec.sizes == []
    # a second fact about one entity changes its page's lead: exactly one section, then the topic
    write_files(src, {"notes/doc007.md": "# Doc 7\n\nAlpha007 is very fast. Alpha007 is portable. Beta007 is reliable.\n"})
    report, rec = compile_with(store, src)
    assert report.n("regenerate_page") == 1
    assert report.n("regenerate_section") == 1 and report.n("cutoff_section") == n_sections - 1
    # the topic is regenerated only if that section's overview changed (early cutoff one level up);
    # otherwise it is re-rendered, since its listing shows the new section overview
    assert report.n("regenerate_summary") + report.n("rerender_summary") == 1 and report.n("cutoff_summary") == 0
    assert len(rec.sizes) == 1 + report.n("regenerate_summary") and max(rec.sizes) <= SECTION_MAX
    equal, detail = verify_against_full(store, src, make_provider("mock"), {})
    assert equal, detail
    # an edit that changes several pages regenerates at most one section per changed page
    write_files(src, {"notes/doc011.md": "# Doc 11\n\nAlpha011 is fast. Gamma011 is reliable.\n"})
    report, _ = compile_with(store, src)
    changed_pages = report.n("regenerate_page") + report.n("delete_page")
    assert 1 <= report.n("regenerate_section") <= changed_pages
    equal, detail = verify_against_full(store, src, make_provider("mock"), {})
    assert equal, detail


def test_dry_run_with_an_llm_provider_plans_sections_and_topic(tmp_path):
    import shutil

    from dakc.workspace import Workspace

    plugins = tmp_path / "plugins"
    plugins.mkdir()
    shutil.copy(Path(__file__).resolve().parents[1] / "fixtures" / "fake_custom_client.py", plugins / "byo_client.py")
    big_topic(tmp_path / "knowledge")
    ws = Workspace.init(tmp_path, provider="custom")
    ws.config.provider_options["custom"] = {"factory": "byo_client:make", "path": "plugins"}
    bundle = ws.provider()
    Compiler(ws.store(), bundle, ws.source_root).run()
    write_files(tmp_path / "knowledge", {"notes/doc007.md": "# Doc 7\n\nAlpha007 is fast. Alpha007 is portable. Beta007 is reliable.\n"})
    plan, _ = Compiler(ws.store(), bundle, ws.source_root, dry_run=True).run()
    assert not plan.exact
    assert plan.planned_llm_calls["summary"] == plan.n("regenerate_section") + plan.n("regenerate_summary")
    assert plan.n("regenerate_section") >= 1 and plan.usage.by_stage.get("summary", 0) == 0  # nothing spent


def test_flat_summaries_from_an_older_version_are_upgraded(tmp_path, monkeypatch):
    import dakc.compiler as compiler

    src = tmp_path / "knowledge"
    big_topic(src)
    store = Store(tmp_path / ".dakc" / "dakc.db")
    monkeypatch.setattr(compiler, "SECTION_FLAT", 10_000)  # behave like summary/1: one flat call
    monkeypatch.setattr(versions, "SUMMARY", "summary/1")
    compile_with(store, src)
    assert store.load_state().of("summary")["sum:notes"].sections == ()
    monkeypatch.undo()
    report, _ = compile_with(store, src)
    assert report.status == "committed" and any("summary structure" in n for n in report.notes)
    assert store.load_state().of("summary")["sum:notes"].sections
    assert report.n("regenerate_page") == 0 and report.usage.by_stage.get("extract", 0) == 0
    store.close()
