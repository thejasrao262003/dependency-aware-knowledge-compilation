"""The MkDocs plugin, exercised through MkDocs' own build in strict mode."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("mkdocs")

from mkdocs.commands.build import build  # noqa: E402
from mkdocs.config import load_config  # noqa: E402
from mkdocs.exceptions import Abort  # noqa: E402

import dakc  # noqa: E402

from tests.conftest import POSTGRES_DOCS, write_files  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def site(root: Path, options: str = "") -> Path:
    write_files(root / "docs", POSTGRES_DOCS)
    plugin = "  - dakc" + (":\n" + "\n".join(f"      {line}" for line in options.strip().splitlines()) if options else "")
    (root / "mkdocs.yml").write_text(f"site_name: Test docs\nplugins:\n  - search\n{plugin}\n", encoding="utf-8")
    return root


def mkdocs_build(root: Path) -> None:
    build(load_config(str(root / "mkdocs.yml"), strict=True))


def test_build_publishes_knowledge_linked_to_the_docs(tmp_path):
    root = site(tmp_path, "min_claims: 1")
    mkdocs_build(root)
    out = root / "site" / "knowledge"
    assert (out / "index.html").exists() and (out / "summaries").is_dir()
    page = (out / "pages" / "jsonb" / "index.html").read_text()
    assert "PostgreSQL supports JSONB" in page
    assert 'href="../../../databases/postgres/"' in page  # the citation links to the docs page it came from
    assert (root / ".dakc-kb" / "dakc.toml").exists() and not (root / "docs" / "knowledge").exists()


def test_incremental_across_builds(tmp_path):
    root = site(tmp_path, "min_claims: 1")
    mkdocs_build(root)
    with dakc.open(root / ".dakc-kb") as kb:
        head = kb.head
    mkdocs_build(root)  # nothing changed
    with dakc.open(root / ".dakc-kb") as kb:
        assert kb.builds(1)[0].status == "noop" and kb.status().up_to_date
    write_files(root / "docs", {"databases/postgres.md": "# PostgreSQL\n\nPostgreSQL supports Transactions. "
                                "PostgreSQL stores JSONB compactly. PostgreSQL supports Indexing.\n"})
    mkdocs_build(root)
    with dakc.open(root / ".dakc-kb") as kb:
        last = kb.builds(1)[0]
        assert last.status == "committed" and last.id > head
        assert sorted(last.stats["operations"]["regenerate_page"]) == ["page:jsonb", "page:postgresql"]
    assert "stores JSONB compactly" in (root / "site" / "knowledge" / "pages" / "jsonb" / "index.html").read_text()


def test_publish_false_compiles_without_publishing(tmp_path):
    root = site(tmp_path, "publish: false")
    mkdocs_build(root)
    assert not (root / "site" / "knowledge").exists()
    with dakc.open(root / ".dakc-kb") as kb:
        assert kb.head >= 1 and kb.store.counts()["page"] > 0


def test_disabled_does_nothing(tmp_path):
    root = site(tmp_path, "enabled: false")
    mkdocs_build(root)
    assert not (root / ".dakc-kb").exists() and not (root / "site" / "knowledge").exists()


def test_misconfiguration_is_refused(tmp_path, caplog):
    with pytest.raises(Abort):
        mkdocs_build(site(tmp_path / "a", "workspace: docs/.kb"))
    assert "must be outside docs_dir" in caplog.text
    root = site(tmp_path / "b")
    write_files(root / "docs", {"knowledge/existing.md": "# Existing\n"})
    with pytest.raises(Abort):
        mkdocs_build(root)
    assert "set the plugin's `prefix` option" in caplog.text


def test_llm_cost_guard_fails_the_build(tmp_path, caplog):
    root = site(tmp_path, "max_llm_calls: 1")
    ws = root / ".dakc-kb"
    (ws / "plugins").mkdir(parents=True)
    shutil.copy(FIXTURES / "fake_custom_client.py", ws / "plugins" / "byo_client.py")
    (ws / "dakc.toml").write_text('[dakc]\nsource_dir = "../docs"\nprovider = "custom"\n\n'
                                  '[provider.custom]\nfactory = "byo_client:make"\npath = "plugins"\n', encoding="utf-8")
    with pytest.raises(Abort):
        mkdocs_build(root)
    assert "above max_llm_calls=1" in caplog.text
    with dakc.open(ws) as kb:
        assert kb.head == 0  # nothing was built or spent


def test_min_claims_publishes_substantial_pages_without_dangling_links(tmp_path):
    root = site(tmp_path)  # default min_claims: 2
    mkdocs_build(root)  # strict: any link to an unpublished page would fail the build
    pages = root / "site" / "knowledge" / "pages"
    assert (pages / "postgresql" / "index.html").exists()  # 4 facts
    assert not (pages / "jsonb").exists()  # 1 fact: kept in the knowledge base, not published
    assert "jsonb" not in (pages / "postgresql" / "index.html").read_text().lower().split("related")[-1].split("sources")[0]
    with dakc.open(root / ".dakc-kb") as kb:
        assert kb.read("page:jsonb").kind == "page"
