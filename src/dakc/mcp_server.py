"""MCP server: a DAKC knowledge base as tools for AI agents (``dakc mcp``).

The server is bound to one workspace when it starts; agents cannot point it at
other directories. Design rules:

* **Inspection tools are read-only** and never take the build lock.
* **``compile`` previews by default.** It only builds with ``apply=true``. With an
  LLM provider it first plans, and refuses a build planned to exceed
  ``max_llm_calls`` (an upper bound, so the guard is conservative).
* **``rollback`` needs an explicit build id** and is marked destructive.
* ``--read-only`` omits ``compile`` and ``rollback`` entirely.
* **Outputs are bounded** (``limit`` / ``max_chars`` arguments) so a hub page or
  a large build cannot flood the agent's context; totals are always reported.

Each tool call opens its own :class:`~dakc.kb.KnowledgeBase`: the SDK runs
synchronous tools on worker threads, and a connection is not shared between them.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from dakc import __version__
from dakc.errors import DakcError
from dakc.kb import KnowledgeBase
from dakc.provenance.trace import PageTrace

Kind = Literal["source", "passage", "claim", "entity", "relationship", "page", "summary", "index"]
ArtifactRef = Annotated[str, Field(description="An artifact id (page:postgresql, clm:..., sum:...), a source path, or a page title.")]
Limit = Annotated[int, Field(ge=1, le=500, description="Maximum items returned per list; totals are always reported.")]

INSTRUCTIONS = """\
DAKC compiles a directory of documents into a knowledge base: claims (facts extracted from source sentences), \
entity pages, topic summaries and an index. It records which source sentences every artifact depends on, \
and rebuilds only what a source change invalidates.

To answer a question from the knowledge base: list_artifacts(kind="page", query=...) then read_artifact. \
Use trace on a page to see which source sentences support each statement, and cite those sources.
To see what an edit to a document would affect, before making it: downstream(<source path>).
After documents change: status, then compile (a preview by default), then compile(apply=true) to build.
explain tells you why an artifact is stale and what it depends on."""

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def _cap(items: list, limit: int) -> list:
    return items[:limit]


def _compact_build(b: dict[str, Any]) -> dict[str, Any]:
    s = b.get("stats") or {}
    return {
        "id": b["id"],
        "parent": b["parent"],
        "strategy": b["strategy"],
        "status": b["status"],
        "started_at": b["started_at"],
        "finished_at": b["finished_at"],
        "error": b["error"],
        "llm_calls": s.get("usage", {}).get("llm_calls"),
        "operation_counts": s.get("operation_counts", {}),
        "artifact_versions_written": s.get("changes", {}).get("artifact_versions_written"),
    }


def create_server(workspace: str | Path = ".", *, read_only: bool = False, max_llm_calls: int = 100) -> MCPServer:
    """Build the MCP server for one workspace. Raises :class:`~dakc.errors.WorkspaceNotFound`
    if the directory has no ``dakc.toml``."""
    root = Path(workspace).resolve()
    with KnowledgeBase.open(root) as kb:  # fail fast on a bad workspace
        provider_free = kb.provider().free
        provider_name = kb.workspace.config.provider

    server = MCPServer(
        "dakc",
        title="DAKC knowledge compiler",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    @contextmanager
    def session() -> Iterator[KnowledgeBase]:
        try:
            with KnowledgeBase.open(root) as kb:
                yield kb
        except DakcError as exc:  # anticipated failures reach the agent verbatim
            raise ToolError(str(exc)) from exc

    # ------------------------------------------------------------ inspection
    @server.tool(annotations=READ_ONLY)
    def status() -> dict[str, Any]:
        """Current build (HEAD), artifact counts, and whether sources changed since it.
        If not up to date, `stale` counts the artifacts downstream of changed sources."""
        with session() as kb:
            return kb.status().to_dict()

    @server.tool(annotations=READ_ONLY)
    def check_sources(limit: Limit = 50) -> dict[str, Any]:
        """Which source documents are new, modified, deleted or renamed since the last build
        (content hashes, not timestamps)."""
        with session() as kb:
            ch = kb.check()
        d = ch.to_dict()
        d["counts"] = {k: len(d[k]) for k in ("new", "modified", "deleted", "renamed")}
        for k in ("new", "modified", "deleted", "renamed", "duplicates"):
            d[k] = _cap(d[k], limit)
        return d

    @server.tool(annotations=READ_ONLY)
    def list_artifacts(
        kind: Annotated[Kind | None, Field(description="Artifact kind; omit for all kinds.")] = "page",
        query: Annotated[str | None, Field(description="Case-insensitive substring of the title (page name, claim text, source path).")] = None,
        limit: Limit = 50,
    ) -> dict[str, Any]:
        """Find artifacts by kind and title. Pages are the readable articles; claims are
        single facts; sources are the original documents."""
        with session() as kb:
            items = kb.artifacts(kind, query=query, limit=limit + 1)
        return {"artifacts": [a.to_dict() for a in items[:limit]], "more_available": len(items) > limit}

    @server.tool(annotations=READ_ONLY)
    def read_artifact(
        artifact: ArtifactRef,
        max_chars: Annotated[int, Field(ge=200, le=200_000, description="Truncate the text after this many characters.")] = 20_000,
    ) -> dict[str, Any]:
        """The current content of an artifact: rendered Markdown for pages, summaries and the
        index (statements carry [^Cn] citation markers; use trace to resolve them), the
        text of claims and passages."""
        with session() as kb:
            c = kb.read(artifact)
        d = c.to_dict()
        d["truncated"] = len(c.text) > max_chars
        d["text"] = c.text[:max_chars]
        for k, v in list(d["metadata"].items()):
            if isinstance(v, list) and len(v) > 50:
                d["metadata"][k] = v[:50]
                d["metadata"][f"{k}_total"] = len(v)
        return d

    @server.tool(annotations=READ_ONLY)
    def explain(artifact: ArtifactRef, limit: Limit = 30) -> dict[str, Any]:
        """What an artifact depends on, what depends on it, the sources upstream of it, and
        whether it is VALID or STALE against the documents on disk (with the reason)."""
        with session() as kb:
            d = kb.explain(artifact).to_dict()
        for k in ("depends_on", "dependents", "upstream_sources", "history"):
            d[f"{k}_total"] = len(d[k])
            d[k] = _cap(d[k], limit)
        return d

    @server.tool(annotations=READ_ONLY)
    def trace(artifact: ArtifactRef, limit: Limit = 50) -> dict[str, Any]:
        """Sentence-level provenance. For a page: each displayed sentence, the claims it cites,
        and for each claim the source document, version and exact evidence sentences. For a
        claim: its evidence. Uncited sentences are listed separately."""
        with session() as kb:
            t = kb.trace(artifact)
        d = t.to_dict()
        if isinstance(t, PageTrace):
            d["sentences_total"], d["claims_total"] = len(d["sentences"]), len(d["claims"])
            d["sentences"], d["claims"] = _cap(d["sentences"], limit), _cap(d["claims"], limit)
        return d

    @server.tool(annotations=READ_ONLY)
    def downstream(artifact: ArtifactRef, limit: Limit = 30) -> dict[str, Any]:
        """Everything that would be invalidated if this source or artifact changed: the
        blast radius of an edit, per artifact kind."""
        with session() as kb:
            return kb.downstream(artifact).to_dict(limit=limit)

    @server.tool(annotations=READ_ONLY)
    def graph_stats() -> dict[str, Any]:
        """Artifact counts per kind and dependency-edge counts per edge kind."""
        with session() as kb:
            return kb.graph_stats()

    @server.tool(annotations=READ_ONLY)
    def builds(limit: Annotated[int, Field(ge=1, le=100, description="Most recent builds to return.")] = 10) -> dict[str, Any]:
        """Build history, newest first: strategy, status (committed, failed, rolled_back),
        LLM calls and operation counts."""
        with session() as kb:
            records, head = kb.builds(limit), kb.head
        return {"head": head, "builds": [_compact_build(b.to_dict()) for b in records]}

    @server.tool(annotations=READ_ONLY)
    def diff_builds(
        a: Annotated[int, Field(ge=0, description="Older build id (0 = empty knowledge base).")],
        b: Annotated[int | None, Field(ge=0, description="Newer build id; omit for HEAD.")] = None,
        limit: Limit = 30,
    ) -> dict[str, Any]:
        """Artifacts created, updated or deleted between two builds."""
        with session() as kb:
            return kb.diff(a, b).to_dict(limit=limit)

    @server.tool(annotations=READ_ONLY)
    def duplicates(limit: Limit = 20) -> dict[str, Any]:
        """Claim pairs that are likely the same fact worded differently. Advisory: DAKC never
        merges claims automatically."""
        with session() as kb:
            pairs = kb.duplicates()
        return {"total": len(pairs), "pairs": [p.to_dict() for p in pairs[:limit]]}

    @server.tool(annotations=READ_ONLY)
    def verify(
        against_full: Annotated[bool, Field(description="Also rebuild from scratch in a temporary store and require equality.")] = False,
    ) -> dict[str, Any]:
        """Check the knowledge base's invariants (edges, acyclicity, provenance, wiki files).
        With against_full, also compare with an independent full rebuild (free providers only
        over MCP)."""
        if against_full and not provider_free:
            raise ToolError(f"a full-rebuild comparison re-pays every {provider_name} call; run "
                            "`dakc verify --against-full --allow-cost` yourself if you want that")
        with session() as kb:
            return kb.verify(against_full=against_full).to_dict()

    if read_only:
        return server

    # -------------------------------------------------------------- mutations
    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                                             openWorldHint=not provider_free))
    def compile(  # noqa: A001 - MCP tool name
        apply: Annotated[bool, Field(description="false: preview only (nothing written or spent). true: build and commit.")] = False,
        full: Annotated[bool, Field(description="Rebuild everything instead of only what changed.")] = False,
        limit: Limit = 30,
    ) -> dict[str, Any]:
        """Compile the knowledge base after documents changed. By default this is a preview:
        what would be re-extracted and regenerated, and how many LLM calls it would make.
        Call again with apply=true to build. Every build is atomic and recorded; previous
        builds stay restorable until a rollback."""
        with session() as kb:
            plan = kb.plan(full=full)
            if not apply:
                return {"applied": False, "plan": plan.to_dict(limit=limit),
                        "next": "call compile with apply=true to build" if plan.has_work()
                        else "nothing to do: the knowledge base is up to date"}
            if not provider_free and plan.llm_calls() > max_llm_calls:
                raise ToolError(f"this build is planned to need up to {plan.llm_calls()} {provider_name} LLM calls, above "
                                f"this server's limit of {max_llm_calls}; run `dakc compile` yourself, or restart the "
                                "server with a higher --max-llm-calls")
            report = kb.compile(full=full, wait=5.0)
            return {"applied": True, "report": report.to_dict(limit=limit)}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
    def rollback(
        to: Annotated[int, Field(ge=0, description="The committed build id to restore (see builds).")],
    ) -> dict[str, Any]:
        """Restore the knowledge base and its wiki files to an earlier committed build.
        Destructive: artifact versions written by later builds are discarded (their build
        records stay in the history as rolled_back). Source documents are not touched."""
        with session() as kb:
            return kb.rollback(to, wait=5.0).to_dict()

    return server
