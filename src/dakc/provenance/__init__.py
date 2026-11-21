"""Explain an artifact: what it depends on, what depends on it, and whether it is stale.

Staleness is computed live: the source tree is scanned (content hashes) and an
artifact is STALE when any source it transitively depends on was modified,
deleted or renamed since the last committed build.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dakc.artifacts import kind_of
from dakc.graph import downstream, titles, upstream
from dakc.ingestion import ScanResult, scan
from dakc.storage import Store


@dataclass
class Explanation:
    id: str
    kind: str
    title: str
    status: str
    reasons: list[str] = field(default_factory=list)
    version: int | None = None
    last_changed_build: int | None = None
    last_changed_at: str | None = None
    depends_on: list[dict[str, str]] = field(default_factory=list)
    upstream_sources: list[dict[str, str]] = field(default_factory=list)
    upstream_counts: dict[str, int] = field(default_factory=dict)
    dependents: list[dict[str, str]] = field(default_factory=list)
    downstream_counts: dict[str, int] = field(default_factory=dict)
    signatures: dict[str, str] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def changed_sources(store: Store, source_root: Path, exclude: tuple[Path, ...] = ()) -> tuple[ScanResult, dict[str, str]]:
    """Scan sources; return the scan and {source id: change} for registered sources that changed."""
    registry = store.load_sources()
    sr = scan(source_root, registry, exclude=exclude)
    changed = {sid: "modified" for sid, _ in sr.modified}
    changed.update({sid: "deleted" for sid in sr.deleted})
    changed.update({sid: "renamed" for sid, _, _ in sr.renamed})
    return sr, changed


def explain(store: Store, artifact_id: str, changed: dict[str, str] | None = None) -> Explanation:
    kind = kind_of(artifact_id)
    cur = store.current_artifact(artifact_id)
    src = store.current_source(artifact_id) if kind == "source" else None
    if cur is None and src is None:
        hist = store.artifact_history(artifact_id)
        return Explanation(artifact_id, kind, artifact_id, "DELETED" if hist else "UNKNOWN", history=hist)

    if src is not None:
        title = src.path
        exp = Explanation(artifact_id, "source", title, "VALID")
        row = store.conn.execute(
            "SELECT from_build FROM sources WHERE id=? AND to_build IS NULL", (artifact_id,)
        ).fetchone()
        exp.last_changed_build = row[0] if row else None
    else:
        art, version, from_build = cur  # type: ignore[misc]
        exp = Explanation(artifact_id, kind, art.title, "VALID", version=version, last_changed_build=from_build)
        payload = art.payload()
        exp.signatures = {k: payload[k] for k in ("gen_sig", "render_sig", "sig") if k in payload and payload[k]}
        exp.history = store.artifact_history(artifact_id)
    if exp.last_changed_build:
        b = store.build(exp.last_changed_build)
        exp.last_changed_at = b.finished_at if b else None

    deps = store.dependencies(artifact_id)
    dep_titles = titles(store, [d for d, _ in deps])
    exp.depends_on = [{"id": d, "kind": kind_of(d), "edge": k, "title": dep_titles.get(d, d)} for d, k in deps]
    dependents = store.dependents(artifact_id)
    dt = titles(store, [d for d, _ in dependents])
    exp.dependents = [{"id": d, "kind": kind_of(d), "edge": k, "title": dt.get(d, d)} for d, k in dependents]

    up = upstream(store, artifact_id)
    up_sources = sorted(i for i in up if kind_of(i) == "source")
    st = titles(store, up_sources)
    exp.upstream_sources = [{"id": s, "path": st.get(s, s)} for s in up_sources]
    for i in up:
        if i != artifact_id:
            exp.upstream_counts[kind_of(i)] = exp.upstream_counts.get(kind_of(i), 0) + 1
    down = downstream(store, artifact_id)
    for i in down:
        if i != artifact_id:
            exp.downstream_counts[kind_of(i)] = exp.downstream_counts.get(kind_of(i), 0) + 1

    if changed:
        hits = [(s, changed[s]) for s in ([artifact_id] if src else up_sources) if s in changed]
        if hits:
            exp.status = "STALE"
            exp.reasons = [f"source {st.get(s, s) if not src else src.path} {why} since build {store.head}" for s, why in hits]
    return exp


def stale_artifacts(store: Store, changed: dict[str, str]) -> dict[str, set[str]]:
    """Every artifact transitively downstream of a changed source, grouped by kind."""
    out: dict[str, set[str]] = {}
    for sid in changed:
        for aid in downstream(store, sid):
            if aid != sid:
                out.setdefault(kind_of(aid), set()).add(aid)
    return out
