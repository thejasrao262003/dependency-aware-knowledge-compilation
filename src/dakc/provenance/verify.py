"""Invariant checks over a store (``dakc verify``)."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from dakc.artifacts import from_payload
from dakc.compiler.projection import MANIFEST, drifted_files
from dakc.graph import build_graph, find_cycle, layering_violations
from dakc.provenance.compare import canonical_state, diff_states
from dakc.storage import Store

if TYPE_CHECKING:
    from dakc.sources import SourceSet


@dataclass
class VerifyResult:
    checks: dict[str, bool] = field(default_factory=dict)
    problems: dict[str, list[str]] = field(default_factory=dict)
    metrics: dict[str, object] = field(default_factory=dict)  # informative, not pass/fail

    @property
    def ok(self) -> bool:
        return all(self.checks.values())

    def add(self, name: str, problems: list[str]) -> None:
        self.checks[name] = not problems
        if problems:
            self.problems[name] = problems[:20]


def verify_store(store: Store, output_root: Path | None = None, state_dir: Path | None = None,
                 conflict_rules: list | None = None, conflict_aliases: dict[str, str] | None = None) -> VerifyResult:
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

    # provenance
    from dakc.claims import split_sentences
    from dakc.provenance.trace import citation_coverage

    passages = {aid: json.loads(p) for aid, p in store.conn.execute("SELECT id, payload FROM artifacts WHERE kind='passage' AND to_build IS NULL")}
    bad_evidence = []
    for aid, payload in store.conn.execute("SELECT id, payload FROM artifacts WHERE kind='claim' AND to_build IS NULL"):
        for s in json.loads(payload)["supports"]:
            ev = s[3] if len(s) > 3 else []
            n = len(split_sentences(passages[s[0]]["text"])) if s[0] in passages else 0
            if any(not 0 <= i < n for i in ev):
                bad_evidence.append(f"{aid} -> {s[0]} {ev}")
    res.add("claim evidence sentences lie inside their passage", bad_evidence)
    cov = citation_coverage(store)
    res.add("page citation markers point to the page's own claims", [f"{cov['invalid_markers']} invalid marker(s)"] if cov["invalid_markers"] else [])
    res.metrics["citation_coverage"] = cov

    if conflict_rules is not None:
        from dakc.conflicts import detect_in_store
        from dakc.conflicts.verifier import verified_in_store
        from dakc.conflicts import fingerprint as conflicts_fingerprint

        if store.get_meta("conflicts_fp") == conflicts_fingerprint(conflict_rules, conflict_aliases):  # else the next compile re-judges all
            want = {f.id: (f.kind, f.payload()) for f in detect_in_store(store, conflict_rules, conflict_aliases).values()}
            want.update(verified_in_store(store, conflict_rules, conflict_aliases))
            have = {i: (k, json.loads(p)) for i, k, p in store.conn.execute(
                "SELECT id, kind, payload FROM conflicts WHERE to_build IS NULL")}
            res.add("conflict findings equal a detection from scratch",
                    [f"{i}: {'missing' if i not in have else 'unexpected' if i not in want else 'differs'}"
                     for i in sorted(set(want) | set(have)) if want.get(i) != have.get(i)])

    if store.get_meta("links_fp"):  # else the next compile checks every link
        from dakc.compiler import link_history
        from dakc.links import DocLinks, check

        st = store.load_state()
        index = {sid: DocLinks.from_json(ln, an) for sid, ln, an in
                 store.conn.execute("SELECT source_id, links, anchors FROM link_index")}
        found = check(index, {sid: r.path for sid, r in st.sources.items()}, *link_history(store.conn, st.sources, st.of("passage")))
        want = {i: b.payload() for i, b in found.items()}
        have = {i: json.loads(p) for i, p in store.conn.execute("SELECT id, payload FROM links WHERE to_build IS NULL")}
        res.add("broken-link findings equal a check from scratch",
                [f"{i}: {'missing' if i not in have else 'unexpected' if i not in want else 'differs'}"
                 for i in sorted(set(want) | set(have)) if want.get(i) != have.get(i)])

    if output_root is not None and state_dir is not None:
        # Reports drift (a wiki file edited or deleted by hand) without repairing it; the next
        # compile restores those files. Every file is read: verify does not trust recorded stats.
        drifted = drifted_files(store, output_root) if (state_dir / MANIFEST).exists() else []
        res.add("wiki files match the store", [f"{how} {output_root / rel}" for rel, how in drifted])
        if drifted:
            res.metrics["wiki_files_drifted"] = [rel for rel, _ in drifted]
    return res


def verify_against_full(store: Store, source_root: Path | SourceSet, provider, aliases: dict[str, str], exclude: tuple[Path, ...] = (),
                        provenance: str = "report", config_fingerprint: str = "") -> tuple[bool, dict]:
    """Rebuild from scratch into a temporary store and compare canonical states."""
    from dakc.compiler import Compiler

    with tempfile.TemporaryDirectory(prefix="dakc-verify-") as tmp:
        ref_store = Store(Path(tmp) / "full.db")
        try:
            Compiler(ref_store, provider, source_root, aliases=aliases, strategy="full", exclude=exclude,
                     provenance=provenance, config_fingerprint=config_fingerprint).run()
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
