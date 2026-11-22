"""Read-only REST API over a workspace (``dakc serve``).

A thin layer over :class:`dakc.kb.KnowledgeBase`: status, check, plan (dry
run), artifacts, explain, trace, downstream, graph (React Flow format for a
frontend), builds and diff. Nothing here mutates the knowledge base; compiling
stays an explicit CLI, Python or MCP action. Each request opens its own
connection and closes it when the response is sent.

There is no authentication yet, so the API is local-only: ``dakc serve``
binds only to a loopback address, and requests whose ``Host`` header is not
local are rejected (this blocks DNS-rebinding attacks from web pages).
"""

# No `from __future__ import annotations`: FastAPI must evaluate the local
# `KB` dependency annotation at definition time.
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from dakc import __version__
from dakc.errors import ArtifactNotFound, InvalidArgument, WorkspaceNotFound
from dakc.kb import KnowledgeBase
from dakc.workspace import Workspace

GRAPH_EXPORT_LIMIT = 20_000  # edges; larger graphs are only exported around one node
LOCAL_HOSTS = ["127.0.0.1", "localhost", "::1", "[::1]"]


def create_app(root: Path) -> FastAPI:
    try:
        ws = Workspace.load(root)
    except FileNotFoundError as exc:
        raise WorkspaceNotFound(str(exc)) from exc
    app = FastAPI(title="DAKC", version=__version__, description="Dependency-Aware Knowledge Compiler (read-only API)")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)

    def open_kb() -> Iterator[KnowledgeBase]:
        kb = KnowledgeBase(ws)
        try:
            yield kb
        finally:
            kb.close()

    KB = Annotated[KnowledgeBase, Depends(open_kb)]

    @app.exception_handler(ArtifactNotFound)
    def _not_found(_: Request, exc: ArtifactNotFound) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(InvalidArgument)
    def _bad_request(_: Request, exc: InvalidArgument) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/status")
    def status(kb: KB):
        return kb.status().to_dict()

    @app.get("/check")
    def check(kb: KB):
        return kb.check().to_dict()

    @app.get("/plan")
    def plan(kb: KB, full: bool = False):
        """Dry-run compilation plan. Free providers give an exact plan; LLM providers an upper bound."""
        return kb.plan(full=full).to_dict(limit=500)

    @app.get("/graph")
    def graph(kb: KB, source: str | None = None, format: str = Query("reactflow", pattern="^(json|reactflow)$")):
        if source is None and kb.store.counts().get("edges", 0) > GRAPH_EXPORT_LIMIT:
            return {"stats": kb.graph_stats(), "note": "graph too large to export whole; pass ?source="}
        return json.loads(kb.export_graph(format, around=source))

    @app.get("/artifacts")
    def artifacts(kb: KB, kind: str | None = None, query: str | None = None, limit: int = Query(100, ge=1, le=1000)):
        return [a.to_dict() for a in kb.artifacts(kind, query=query, limit=limit)]

    # The more specific routes must be registered before /artifacts/{id}.
    @app.get("/artifacts/{artifact_id:path}/content", response_class=PlainTextResponse)
    def artifact_content(artifact_id: str, kb: KB):
        return kb.read(artifact_id).text

    @app.get("/artifacts/{artifact_id:path}/trace")
    def artifact_trace(artifact_id: str, kb: KB):
        return kb.trace(artifact_id).to_dict()

    @app.get("/artifacts/{artifact_id:path}/downstream")
    def artifact_downstream(artifact_id: str, kb: KB, limit: int = Query(200, ge=1)):
        return kb.downstream(artifact_id).to_dict(limit=limit)

    @app.get("/artifacts/{artifact_id:path}")
    def artifact(artifact_id: str, kb: KB):
        return kb.explain(artifact_id).to_dict()

    @app.get("/builds")
    def builds(kb: KB, limit: int = 50):
        return [b.to_dict() for b in kb.builds(limit)]

    @app.get("/diff/{a}/{b}")
    def diff(a: int, b: int, kb: KB):
        return kb.diff(a, b).changes

    @app.get("/duplicates")
    def duplicates(kb: KB, limit: int = Query(100, ge=1)):
        return [p.to_dict() for p in kb.duplicates(limit)]

    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__}

    _ = HTTPException  # re-exported for callers that extend the app
    return app
