"""Invariant checks over a store (``dakc verify``)."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from dakc.artifacts import from_payload
from dakc.compiler.projection import MANIFEST, rel_path
from dakc.graph import build_graph, find_cycle, layering_violations
from dakc.hashing import h
from dakc.provenance.compare import canonical_state, diff_states
from dakc.storage import Store


@dataclass
class VerifyResult:
    checks: dict[str, bool] = field(default_factory=dict)
    problems: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(self.checks.values())

    def add(self, name: str, problems: list[str]) -> None:
        self.checks[name] = not problems
        if problems:
            self.problems[name] = problems[:20]


def verify_store(store: Store, output_root: Path | None = None, state_dir: Path | None = None) -> VerifyResult:
    res = VerifyResult()
    current = {r[0] for r in store.conn.execute("SELECT id FROM artifacts WHERE to_build IS NULL")}
    current |= {r[0] for r in store.conn.execute("SELECT id FROM sources WHERE to_build IS NULL")}
    edges = store.current_edges()

    res.add("no dangling edges", [f"{a} -> {b}" for a, b, _ in edges if a not in current or b not in current])
    res.add("edges point to lower layers (acyclic by construction)", [f"{a} -> {b}" for a, b in layering_violations(store)])
    if len(edges) <= 200_000:
        cyc = find_cycle(build_graph(store))
        res.add("no dependency cycles (NetworkX)", [str(cyc)] if cyc else [])

    stored: dict[str, set[tuple[str, str]]] = {}
    for a, b, k in edges:
        stored.setdefault(a, set()).add((b, k))
    mismatched, empty_claims, empty_entities = [], [], []
    for aid, kind, payload in store.conn.execute("SELECT id, kind, payload FROM artifacts WHERE to_build IS NULL"):
        art = from_payload(kind, aid, json.loads(payload))
        if set(art.deps()) != stored.get(aid, set()):
            mismatched.append(aid)
        if kind == "claim" and not art.supports:  # type: ignore[attr-defined]
            empty_claims.append(aid)
        if kind in ("entity", "relationship") and not art.claim_ids:  # type: ignore[attr-defined]
            empty_entities.append(aid)
    res.add("stored edges equal edges derived from artifact content", mismatched)
    res.add("no valid claim without support", empty_claims)
    res.add("no entity/relationship without claims", empty_entities)

    if output_root is not None and state_dir is not None:
        problems = []
        manifest_path = state_dir / MANIFEST
        if manifest_path.exists():
            for aid, kind, payload in store.conn.execute(
                "SELECT id, kind, payload FROM artifacts WHERE to_build IS NULL AND kind IN ('page','summary','index')"
            ):
                p = output_root / rel_path(aid, kind)
                if not p.exists():
                    problems.append(f"missing {p}")
                elif h(p.read_text(encoding="utf-8")) != h(json.loads(payload)["rendered"]):
                    problems.append(f"differs {p}")
        res.add("wiki files match the store", problems)
    return res


def verify_against_full(store: Store, source_root: Path, provider, aliases: dict[str, str], exclude: tuple[Path, ...] = ()) -> tuple[bool, dict]:
    """Rebuild from scratch into a temporary store and compare canonical states."""
    from dakc.compiler import Compiler

    with tempfile.TemporaryDirectory(prefix="dakc-verify-") as tmp:
        ref_store = Store(Path(tmp) / "full.db")
        try:
            Compiler(ref_store, provider, source_root, aliases=aliases, strategy="full", exclude=exclude).run()
            ref, cand = canonical_state(ref_store), canonical_state(store)
        finally:
            ref_store.close()
    if not provider.free:
        # Non-deterministic providers: claims re-extracted from scratch need not
        # match, so only the deterministic layers (sources, passages and their
        # edges) are compared. Nothing is claimed about generated content.
        keep = ("source:", "passage:", "edge:psg@")
        ref = {k: v for k, v in ref.items() if k.startswith(keep)}
        cand = {k: v for k, v in cand.items() if k.startswith(keep)}
    d = diff_states(ref, cand)
    return d.equal, {"by_kind": d.by_kind(), "missing": d.missing[:20], "extra": d.extra[:20], "different": d.different[:20], "content_compared": provider.free}
