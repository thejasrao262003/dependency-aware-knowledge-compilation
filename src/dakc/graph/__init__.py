"""Dependency graph: traversal, validation and export.

Traversal for the hot path is SQL over indexed edge tables (fast at any size).
NetworkX is used to materialize subgraphs for cycle detection, topological
ordering, statistics and export (node-link JSON, Graphviz DOT, React Flow).
Edges point from a dependent artifact to the artifact it depends on.
"""

from __future__ import annotations

import json
from collections import Counter, deque

import networkx as nx

from dakc.artifacts import LAYER, kind_of
from dakc.hashing import entity_key, slugify
from dakc.storage import Store


def resolve_id(store: Store, ident: str) -> str | None:
    """Accept exact ids and friendly forms: ``page:database-architecture``,
    ``entity_postgresql``, ``source:notes/db.md``, a source path, or a page title."""
    ident = ident.strip()
    if store.current_artifact(ident) or store.current_source(ident):
        return ident
    src = store.conn.execute("SELECT id FROM sources WHERE path=? AND to_build IS NULL", (ident,)).fetchone()
    if src:
        return src[0]
    head, sep, rest = ident.replace("_", ":", 1).partition(":") if ":" not in ident else ident.partition(":")
    if sep:
        head = head.lower()
        if head in ("page", "entity", "ent"):
            cand = ("page:" if head == "page" else "ent:") + entity_key(rest)
            if store.current_artifact(cand):
                return cand
        if head in ("summary", "sum", "topic"):
            cand = "sum:" + slugify(rest)
            if store.current_artifact(cand):
                return cand
        if head in ("source", "src"):
            r = store.conn.execute("SELECT id FROM sources WHERE (path=? OR id=?) AND to_build IS NULL", (rest, "src:" + slugify(rest))).fetchone()
            if r:
                return r[0]
        if head in ("claim", "clm"):
            cand = "clm:" + rest
            if store.current_artifact(cand):
                return cand
    for aid, payload in store.conn.execute("SELECT id, payload FROM artifacts WHERE kind='page' AND to_build IS NULL"):
        if json.loads(payload)["title"].casefold() == ident.casefold():
            return aid
    return None


def _bfs(store: Store, root: str, direction: str, max_depth: int | None = None) -> dict[str, int]:
    depth = {root: 0}
    frontier = [root]
    while frontier:
        if max_depth is not None and depth[frontier[0]] >= max_depth:
            break
        rows = store.dependents_many(frontier) if direction == "down" else store.dependencies_many(frontier)
        nxt: list[str] = []
        for dependent, dependency, _kind in rows:
            other = dependent if direction == "down" else dependency
            if other not in depth:
                depth[other] = depth[dependent if direction == "up" else dependency] + 1
                nxt.append(other)
        frontier = nxt
    return depth


def downstream(store: Store, root: str, max_depth: int | None = None) -> dict[str, int]:
    """Everything that (transitively) depends on ``root``: id -> distance."""
    return _bfs(store, root, "down", max_depth)


def upstream(store: Store, root: str, max_depth: int | None = None) -> dict[str, int]:
    """Everything ``root`` (transitively) depends on: id -> distance."""
    return _bfs(store, root, "up", max_depth)


def titles(store: Store, ids: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        q = f"SELECT id, kind, payload FROM artifacts WHERE to_build IS NULL AND id IN ({','.join('?' * len(chunk))})"
        for aid, kind, payload in store.conn.execute(q, chunk):
            p = json.loads(payload)
            if kind in ("page", "summary"):
                out[aid] = p["title"]
            elif kind == "entity":
                out[aid] = p["name"]
            elif kind == "claim":
                out[aid] = min(s[1] for s in p["supports"]) if p["supports"] else aid
            elif kind == "passage":
                out[aid] = p["text"][:80]
            elif kind == "relationship":
                out[aid] = aid.split(":", 1)[1].replace("--", " ↔ ")
            else:
                out[aid] = "Index"
        q = f"SELECT id, path FROM sources WHERE to_build IS NULL AND id IN ({','.join('?' * len(chunk))})"
        for sid, path in store.conn.execute(q, chunk):
            out[sid] = path
    return out


def build_graph(store: Store, ids: set[str] | None = None, with_titles: bool = False) -> nx.DiGraph:
    """Materialize the current graph (or the subgraph induced by ``ids``)."""
    g = nx.DiGraph()
    if ids is None:
        for sid, path in store.conn.execute("SELECT id, path FROM sources WHERE to_build IS NULL"):
            g.add_node(sid, kind="source", label=path)
        for aid, kind in store.conn.execute("SELECT id, kind FROM artifacts WHERE to_build IS NULL"):
            g.add_node(aid, kind=kind, label=aid)
        for dependent, dependency, kind in store.current_edges():
            g.add_edge(dependent, dependency, kind=kind)
    else:
        for i in ids:
            g.add_node(i, kind=kind_of(i), label=i)
        for dependent, dependency, kind in store.dependencies_many(ids):
            if dependency in ids:
                g.add_edge(dependent, dependency, kind=kind)
    if with_titles:
        for i, t in titles(store, list(g.nodes)).items():
            g.nodes[i]["label"] = t
    return g


def layering_violations(store: Store) -> list[tuple[str, str]]:
    """Edges that do not point to a strictly lower layer (would allow cycles)."""
    bad = []
    for dependent, dependency, _ in store.current_edges():
        if LAYER.get(kind_of(dependency), 99) >= LAYER.get(kind_of(dependent), -1):
            bad.append((dependent, dependency))
    return bad


def find_cycle(g: nx.DiGraph) -> list[tuple[str, str]] | None:
    try:
        return list(nx.find_cycle(g))
    except nx.NetworkXNoCycle:
        return None


def execution_order(g: nx.DiGraph) -> list[list[str]]:
    """Topological generations from inputs upwards (dependencies first)."""
    return [sorted(gen) for gen in nx.topological_generations(g.reverse(copy=False))]


def stats(store: Store) -> dict[str, dict[str, int]]:
    nodes = Counter({"source": store.conn.execute("SELECT COUNT(*) FROM sources WHERE to_build IS NULL").fetchone()[0]})
    for kind, n in store.conn.execute("SELECT kind, COUNT(*) FROM artifacts WHERE to_build IS NULL GROUP BY kind"):
        nodes[kind] = n
    edges = Counter()
    for kind, n in store.conn.execute("SELECT kind, COUNT(*) FROM edges WHERE to_build IS NULL GROUP BY kind"):
        edges[kind] = n
    return {"nodes": dict(sorted(nodes.items(), key=lambda kv: LAYER.get(kv[0], 9))), "edges": dict(sorted(edges.items()))}


def export(g: nx.DiGraph, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(nx.node_link_data(g, edges="edges"), indent=1, sort_keys=True)
    if fmt == "dot":
        lines = ["digraph dakc {", "  rankdir=BT;", "  node [shape=box, fontsize=10];"]
        for n, d in sorted(g.nodes(data=True)):
            label = str(d.get("label", n)).replace('"', "'")[:60]
            lines.append(f'  "{n}" [label="{label}\\n({d.get("kind")})"];')
        for a, b, d in sorted(g.edges(data=True)):
            lines.append(f'  "{a}" -> "{b}" [label="{d.get("kind", "")}"];')
        lines.append("}")
        return "\n".join(lines) + "\n"
    if fmt == "reactflow":
        nodes, rows = [], Counter()
        for n, d in sorted(g.nodes(data=True), key=lambda t: (LAYER.get(t[1].get("kind"), 9), t[0])):
            layer = LAYER.get(d.get("kind"), 7)
            nodes.append({
                "id": n,
                "type": "default",
                "data": {"label": d.get("label", n), "kind": d.get("kind")},
                "position": {"x": rows[layer] * 220, "y": layer * 140},
            })
            rows[layer] += 1
        edges = [{"id": f"{a}->{b}", "source": b, "target": a, "label": d.get("kind", "")} for a, b, d in sorted(g.edges(data=True))]
        return json.dumps({"nodes": nodes, "edges": edges}, indent=1)
    raise ValueError(f"unknown format {fmt!r} (json, dot, reactflow)")
