"""Read-only REST API over a workspace (``dakc serve``).

Endpoints mirror the CLI: status, check, plan (dry run), graph (React Flow
format for a frontend), explain, builds and diff. Nothing here mutates the
knowledge base; compiling stays an explicit CLI action.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse

from dakc.compiler import Compiler
from dakc.graph import build_graph, downstream, export, resolve_id, stats
from dakc.provenance import changed_sources, explain
from dakc.workspace import Workspace


def create_app(root: Path) -> FastAPI:
    ws = Workspace.load(root)
    app = FastAPI(title="DAKC", version="0.1.0", description="Dependency-Aware Knowledge Compiler (read-only API)")

    def store():
        return ws.store()

    @app.get("/status")
    def status():
        st = store()
        sr, changed = changed_sources(st, ws.source_root, ws.excluded)
        return {"head": st.head, "counts": st.counts(), "source_changes": sr.summary(), "stale_sources": sorted(changed)}

    @app.get("/check")
    def check():
        st = store()
        sr, _ = changed_sources(st, ws.source_root, ws.excluded)
        reg = st.load_sources()
        return {
            "new": sr.new,
            "modified": [p for _, p in sr.modified],
            "deleted": [reg[s].path for s in sr.deleted],
            "renamed": [[o, n] for _, o, n in sr.renamed],
            "unchanged": len(sr.unchanged),
        }

    @app.get("/plan")
    def plan():
        """Dry-run compilation plan. Free providers give an exact plan; LLM providers an upper bound."""
        st = store()
        report, _ = Compiler(
            st, ws.provider(), ws.source_root, aliases=ws.config.alias_map,
            config_fingerprint=ws.config.fingerprint, dry_run=True, exclude=ws.excluded,
        ).run()
        return report.to_dict(limit=500)

    @app.get("/graph")
    def graph(source: str | None = None, format: str = Query("reactflow", pattern="^(json|reactflow)$")):
        st = store()
        if source is None:
            if st.counts().get("edges", 0) > 20_000:
                return {"stats": stats(st), "note": "graph too large to export whole; pass ?source="}
            return json.loads(export(build_graph(st, with_titles=True), format))
        root = resolve_id(st, source)
        if root is None:
            raise HTTPException(404, f"no artifact or source matches {source!r}")
        return json.loads(export(build_graph(st, set(downstream(st, root)), with_titles=True), format))

    # The more specific route must be registered first.
    @app.get("/artifacts/{artifact_id:path}/content", response_class=PlainTextResponse)
    def artifact_content(artifact_id: str):
        st = store()
        aid = resolve_id(st, artifact_id) or artifact_id
        cur = st.current_artifact(aid)
        if cur is None:
            raise HTTPException(404, f"no artifact matches {artifact_id!r}")
        payload = cur[0].payload()
        return payload.get("rendered") or payload.get("text") or json.dumps(payload, indent=1)

    @app.get("/artifacts/{artifact_id:path}")
    def artifact(artifact_id: str):
        st = store()
        aid = resolve_id(st, artifact_id) or artifact_id
        _, changed = changed_sources(st, ws.source_root, ws.excluded)
        exp = explain(st, aid, changed)
        if exp.status == "UNKNOWN":
            raise HTTPException(404, f"no artifact matches {artifact_id!r}")
        return exp.to_dict()

    @app.get("/builds")
    def builds(limit: int = 50):
        return [b.__dict__ for b in store().builds(limit)]

    @app.get("/diff/{a}/{b}")
    def diff(a: int, b: int):
        return store().diff_builds(a, b)

    return app
