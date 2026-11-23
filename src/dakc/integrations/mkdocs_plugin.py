"""MkDocs plugin: compile a knowledge base from the docs on every build.

Add one line to an existing ``mkdocs.yml``; nothing else in the docs workflow
changes::

    plugins:
      - search
      - dakc                     # pip install 'dakc[mkdocs]'

On every ``mkdocs build`` (and every live reload of ``mkdocs serve``) DAKC
compiles the docs incrementally: only what the edits invalidated is rebuilt.
The generated knowledge (entity pages, topic summaries, an index) is
published under ``/knowledge/``, and every cited fact links back to the docs
page it came from.

Options (all optional)::

    - dakc:
        workspace: .dakc-kb      # DAKC state, next to mkdocs.yml; cache this directory in CI
        provider: heuristic      # used when the workspace is created: heuristic | claude-code | anthropic | openai | gemini | ... (see the README)
        publish: true            # add the generated pages to the site
        prefix: knowledge        # where they appear in the site
        min_claims: 2            # publish entity pages backed by at least this many facts (all stay in the KB)
        max_llm_calls: 200       # fail the build if a compile is planned to exceed this (LLM providers)
        verify: false            # fail the build if DAKC's invariant checks fail
        enabled: true            # e.g. !ENV [DAKC_ENABLED, true]
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path, PurePosixPath

from mkdocs.config import base
from mkdocs.config import config_options as c
from mkdocs.config.defaults import MkDocsConfig
from mkdocs.exceptions import PluginError
from mkdocs.plugins import BasePlugin, get_plugin_logger
from mkdocs.structure.files import File, Files, InclusionLevel

from dakc.compiler.projection import rel_path
from dakc.errors import BuildLocked, CompileFailed, DakcError
from dakc.kb import KnowledgeBase
from dakc.workspace import CONFIG_NAME, Workspace

log = get_plugin_logger(__name__)
_CITATION = re.compile(r"^(\[\^C\d+\]: )(.+)$")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+\.md)\)")
_LIST_ITEM_LINK = re.compile(r"^\s*[-*] \[[^\]]+\]\(([^)\s]+\.md)\)")


class DakcPluginConfig(base.Config):
    workspace = c.Type(str, default=".dakc-kb")
    provider = c.Optional(c.Type(str))
    publish = c.Type(bool, default=True)
    prefix = c.Type(str, default="knowledge")
    min_claims = c.Type(int, default=2)
    max_llm_calls = c.Optional(c.Type(int))
    verify = c.Type(bool, default=False)
    enabled = c.Type(bool, default=True)


class DakcPlugin(BasePlugin[DakcPluginConfig]):
    def __init__(self) -> None:
        self.workspace: Path | None = None

    # ------------------------------------------------------------------ config
    def on_config(self, config: MkDocsConfig) -> MkDocsConfig | None:
        if not self.config.enabled:
            return None
        root = Path(config.config_file_path).resolve().parent
        docs = Path(config.docs_dir).resolve()
        ws = (root / self.config.workspace).resolve()
        if ws == docs or docs in ws.parents:
            raise PluginError(f"dakc: the workspace {ws} must be outside docs_dir ({docs}), "
                              "or MkDocs would publish DAKC's own files as documentation")
        prefix = self.config.prefix.strip("/")
        if self.config.publish and (docs / prefix).exists():
            raise PluginError(f"dakc: docs_dir already has a '{prefix}' folder; set the plugin's `prefix` option")
        if not (ws / CONFIG_NAME).exists():
            src = Path(_relative(docs, ws))
            Workspace.init(ws, source_dir=src.as_posix(), provider=self.config.provider or "heuristic")
            log.info(f"created DAKC workspace {ws} for {docs}")
        self.workspace = ws
        if self.config.publish and "footnotes" not in config.markdown_extensions:
            config.markdown_extensions.append("footnotes")  # generated pages cite sources with footnotes
        return config

    # ------------------------------------------------------------------- build
    def on_pre_build(self, *, config: MkDocsConfig) -> None:
        if not self.config.enabled or self.workspace is None:
            return
        try:
            with KnowledgeBase.open(self.workspace) as kb:
                if not kb.provider().free and self.config.max_llm_calls is not None:
                    planned = kb.plan().llm_calls()
                    if planned > self.config.max_llm_calls:
                        raise PluginError(f"dakc: this build is planned to need up to {planned} LLM calls, above "
                                          f"max_llm_calls={self.config.max_llm_calls}; run `dakc compile` yourself "
                                          "or raise the limit")
                report = kb.compile(wait=60.0)
                if self.config.verify:
                    v = kb.verify()
                    if not v.ok:
                        raise PluginError(f"dakc: invariant checks failed: {v.problems}")
        except (CompileFailed, BuildLocked, DakcError) as exc:
            raise PluginError(f"dakc: {exc}") from exc
        ops = report.ops
        if report.status == "noop":
            log.info("knowledge base up to date (nothing to rebuild)")
        else:
            log.info(
                f"build {report.build_id}: {len(ops.get('reextract', []))} passage(s) re-extracted, "
                f"{len(ops.get('regenerate_page', []))} page(s) regenerated, "
                f"{len(ops.get('cutoff_page', []))} checked and unchanged, {report.usage.llm_calls} LLM call(s)"
            )

    def on_files(self, files: Files, /, *, config: MkDocsConfig) -> Files | None:
        if not self.config.enabled or not self.config.publish or self.workspace is None:
            return None
        out = Workspace.load(self.workspace).output_root
        prefix = self.config.prefix.strip("/")
        hidden = self._unpublished_pages()
        for path in sorted(out.rglob("*.md")):
            rel = path.relative_to(out).as_posix()
            if rel in hidden:
                continue
            uri = f"{prefix}/{rel}"
            text = _prune(path.read_text(encoding="utf-8"), rel, hidden) if hidden else path.read_text(encoding="utf-8")
            content = _link_sources(text, uri, files)
            files.append(File.generated(config, uri, content=content, inclusion=InclusionLevel.NOT_IN_NAV))
        return files

    def _unpublished_pages(self) -> set[str]:
        """Wiki paths of entity pages backed by fewer than ``min_claims`` facts."""
        if self.config.min_claims <= 1 or self.workspace is None:
            return set()
        with KnowledgeBase.open(self.workspace) as kb:
            rows = kb.store.conn.execute(
                "SELECT id, json_array_length(json_extract(payload, '$.claim_ids')) FROM artifacts "
                "WHERE kind='page' AND to_build IS NULL").fetchall()
        return {rel_path(pid, "page") for pid, n in rows if (n or 0) < self.config.min_claims}


def _relative(target: Path, start: Path) -> str:
    import os

    return os.path.relpath(target, start)


def _prune(text: str, rel: str, hidden: set[str]) -> str:
    """Remove references to unpublished pages: list items that link to one are
    dropped, other links to one become plain text, so no link is left dangling."""
    here = posixpath.dirname(rel)

    def target(href: str) -> str:
        return posixpath.normpath(posixpath.join(here, href))

    kept = []
    for line in text.splitlines():
        m = _LIST_ITEM_LINK.match(line)
        if m and target(m.group(1)) in hidden:
            continue
        kept.append(_LINK.sub(lambda l: l.group(1) if target(l.group(2)) in hidden else l.group(0), line))
    return "\n".join(kept) + "\n"


def _link_sources(text: str, uri: str, files: Files) -> str:
    """Turn ``[^C1]: a.md, b.md`` citations into links to those docs pages."""
    up = "../" * (len(PurePosixPath(uri).parts) - 1)
    lines = []
    for line in text.splitlines():
        m = _CITATION.match(line)
        if m:
            paths = [p.strip() for p in m.group(2).split(", ")]
            line = m.group(1) + ", ".join(f"[{p}]({up}{p})" if files.get_file_from_path(p) else p for p in paths)
        lines.append(line)
    return "\n".join(lines) + "\n"
