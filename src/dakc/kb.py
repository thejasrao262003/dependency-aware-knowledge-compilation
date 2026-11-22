"""Python API: one :class:`KnowledgeBase` object per workspace.

.. code-block:: python

    import dakc

    with dakc.open("path/to/workspace") as kb:
        plan = kb.plan()                  # what a compile would do; nothing written or spent
        report = kb.compile()             # incremental build; the wiki is updated
        page = kb.read("page:postgresql")
        for s in kb.trace("page:postgresql").sentences:
            print(s.sentence, s.cites)

Every method returns a dataclass with ``to_dict()`` (JSON-safe). The CLI, the
REST API and the MCP server are thin layers over this module.

Concurrency: ``compile`` and ``rollback`` hold the workspace build lock, so
builds from several processes are serialized. Read methods never take it, and
SQLite's WAL mode gives them a consistent snapshot while a build runs. A
``KnowledgeBase`` holds one SQLite connection: close it (or use ``with``), and
use one instance per thread.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dakc.artifacts import KINDS, LAYER, kind_of
from dakc.compiler import STRATEGIES, BuildReport, Compiler
from dakc.compiler.projection import sync_projection
from dakc.errors import (
    ArtifactNotFound,
    CompileFailed,
    CostNotAllowed,
    InvalidArgument,
    RollbackError,
    WorkspaceNotFound,
)
from dakc.graph import build_graph, downstream, export, resolve_id, stats, titles
from dakc.hashing import hash_file
from dakc.parsing import ParseError, parse_bytes
from dakc.provenance import Explanation, changed_sources, explain, stale_artifacts
from dakc.provenance.trace import ClaimTrace, PageTrace, trace_claim, trace_page
from dakc.providers import ProviderBundle
from dakc.storage import BuildRecord, Store
from dakc.transactions import FaultInjector, rollback
from dakc.transactions.lock import workspace_lock
from dakc.workspace import CONFIG_NAME, Workspace

GRAPH_FORMATS = ("json", "dot", "reactflow")


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #
@dataclass
class SourceChanges:
    """Sources on disk compared with the last committed build (content hashes)."""

    root: str
    new: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)  # (old path, new path)
    unchanged: int = 0
    duplicates: list[list[str]] = field(default_factory=list)  # paths with identical bytes

    @property
    def has_changes(self) -> bool:
        return bool(self.new or self.modified or self.deleted or self.renamed)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["renamed"] = [list(r) for r in self.renamed]
        d["has_changes"] = self.has_changes
        return d


@dataclass
class Status:
    workspace: str
    source_root: str
    output_root: str
    provider: str
    head: int
    counts: dict[str, int]
    up_to_date: bool
    source_changes: dict[str, int]
    stale: dict[str, int] = field(default_factory=dict)  # kind -> artifacts downstream of changed sources
    new_sources: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ArtifactSummary:
    id: str
    kind: str
    title: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ArtifactContent:
    """The current version of one artifact: what a reader sees, plus its metadata."""

    id: str
    kind: str
    title: str
    version: int | None
    built_in: int | None  # the build that wrote this version
    text: str  # rendered Markdown for pages, summaries and the index; the text of claims and passages
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Downstream:
    """Everything that (transitively) depends on one source or artifact."""

    root: str
    title: str
    by_kind: dict[str, list[ArtifactSummary]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.by_kind.values())

    def to_dict(self, limit: int | None = None) -> dict[str, Any]:
        return {
            "root": self.root,
            "title": self.title,
            "total": self.total,
            "counts": {k: len(v) for k, v in self.by_kind.items()},
            "by_kind": {k: [a.to_dict() for a in v[:limit]] for k, v in self.by_kind.items()},
        }


@dataclass
class BuildDiff:
    a: int
    b: int
    changes: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # kind -> [(id, created|updated|deleted)]

    def to_dict(self, limit: int | None = None) -> dict[str, Any]:
        return {
            "a": self.a,
            "b": self.b,
            "counts": {k: len(v) for k, v in self.changes.items()},
            "changes": {k: [{"id": i, "change": c} for i, c in v[:limit]] for k, v in self.changes.items()},
        }


@dataclass
class DuplicatePair:
    a: str
    b: str
    a_text: str
    b_text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VerifyReport:
    ok: bool
    checks: dict[str, bool]
    problems: dict[str, list[str]]
    metrics: dict[str, Any]
    equal_to_full_rebuild: bool | None = None  # only with against_full=True
    full_rebuild_diff: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.equal_to_full_rebuild is None:
            d.pop("equal_to_full_rebuild")
            d.pop("full_rebuild_diff")
        return d


@dataclass
class RollbackResult:
    restored_build: int
    new_build: int
    projection: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def open_kb(workspace: str | Path = ".") -> KnowledgeBase:
    """Open an existing workspace (a directory holding ``dakc.toml``)."""
    return KnowledgeBase.open(workspace)


def init(
    workspace: str | Path = ".",
    *,
    source_dir: str = "knowledge",
    output_dir: str = "wiki",
    provider: str = "heuristic",
) -> KnowledgeBase:
    """Create ``dakc.toml`` in ``workspace`` (an existing configuration is kept) and open it."""
    return KnowledgeBase(Workspace.init(Path(workspace), source_dir=source_dir, provider=provider, output_dir=output_dir))


def ingest(
    directory: str | Path,
    workspace: str | Path = ".",
    *,
    provider: str | None = None,
    jobs: int | None = None,
    wait: float = 0.0,
) -> BuildReport:
    """Register ``directory`` as the workspace's sources (if the workspace is new) and compile.

    An existing workspace keeps its configured source directory; the report then
    carries a note saying which directory was compiled.
    """
    directory = Path(directory).resolve()
    if not directory.is_dir():
        raise InvalidArgument(f"{directory} is not a directory")
    root = Path(workspace).resolve()
    if not (root / CONFIG_NAME).exists():
        try:
            rel = directory.relative_to(root).as_posix()
        except ValueError:
            rel = str(directory)
        Workspace.init(root, source_dir=rel, provider=provider or "heuristic")
    with KnowledgeBase.open(root) as kb:
        report = kb.compile(provider=provider, jobs=jobs, wait=wait)
        if kb.source_root != directory:
            report.notes.insert(0, f"workspace source_dir is {kb.source_root}; that directory was compiled")
        return report


# --------------------------------------------------------------------------- #
class KnowledgeBase:
    """A compiled knowledge base: sources, derived artifacts, dependency graph and build history."""

    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self._store: Store | None = None
        self._bundles: dict[str | None, ProviderBundle] = {}

    @classmethod
    def open(cls, workspace: str | Path = ".") -> KnowledgeBase:
        try:
            return cls(Workspace.load(Path(workspace)))
        except FileNotFoundError as exc:
            raise WorkspaceNotFound(str(exc)) from exc

    # ---------------------------------------------------------------- lifecycle
    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = self.workspace.store()
        return self._store

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    def __enter__(self) -> KnowledgeBase:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"KnowledgeBase({str(self.root)!r})"

    # -------------------------------------------------------------- properties
    @property
    def root(self) -> Path:
        return self.workspace.root

    @property
    def source_root(self) -> Path:
        return self.workspace.source_root

    @property
    def output_root(self) -> Path:
        return self.workspace.output_root

    @property
    def head(self) -> int:
        """The current (HEAD) build id; 0 before the first build."""
        return self.store.head

    def provider(self, name: str | None = None) -> ProviderBundle:
        if name not in self._bundles:
            self._bundles[name] = self.workspace.provider(name)
        return self._bundles[name]

    # ----------------------------------------------------------------- helpers
    def resolve(self, ident: str) -> str:
        """Canonical id for an artifact id, a friendly form (``page:PostgreSQL``,
        ``entity_postgresql``), a source path or a page title. A path given
        relative to another directory (``research/notes.md`` for the source
        ``notes.md``) resolves when exactly one source matches it."""
        aid = self._lookup(ident)
        if aid is None:
            raise ArtifactNotFound(self._not_found(ident))
        return aid

    def _source_paths(self) -> dict[str, str]:
        return {path: sid for sid, path in self.store.conn.execute("SELECT id, path FROM sources WHERE to_build IS NULL")}

    def _lookup(self, ident: str) -> str | None:
        aid = resolve_id(self.store, ident)
        if aid is not None:
            return aid
        norm = ident.strip().removeprefix("./")
        hits = [sid for path, sid in self._source_paths().items()
                if norm == path or norm.endswith("/" + path) or path.endswith("/" + norm)]
        return hits[0] if len(hits) == 1 else None

    def _not_found(self, ident: str) -> str:
        msg = f"no source or artifact matches {ident!r}"
        if re.fullmatch(r"(psg|clm):[0-9a-f]+", ident.strip()):
            return (msg + " in the current build. Passage and claim ids are content-addressed: an id shown by a "
                    "compile preview exists only once that build is applied, and an id from an earlier build may "
                    "have been replaced (explain shows its history)")
        norm = ident.strip().removeprefix("./")
        paths = list(self._source_paths())
        suffix = [p for p in paths if norm.endswith("/" + p) or p.endswith("/" + norm)]
        pages = [t for (t,) in self.store.conn.execute(
            "SELECT json_extract(payload, '$.title') FROM artifacts WHERE kind='page' AND to_build IS NULL")]
        close = suffix or difflib.get_close_matches(norm, paths + pages, n=3, cutoff=0.6)
        return msg + (f"; did you mean {', '.join(repr(c) for c in close[:3])}?" if close else "")

    def _changed(self) -> dict[str, str]:
        return changed_sources(self.store, self.source_root, self.workspace.excluded)[1]

    def _compiler(self, strategy: str, *, dry_run: bool, provider: str | None, jobs: int | None,
                  fault: str | None = None) -> Compiler:
        if strategy not in STRATEGIES:
            raise InvalidArgument(f"strategy must be one of {', '.join(STRATEGIES)}")
        ws = self.workspace
        return Compiler(
            self.store, self.provider(provider), ws.source_root,
            aliases=ws.config.alias_map, config_fingerprint=ws.config.fingerprint,
            strategy=strategy, jobs=jobs or ws.config.jobs, dry_run=dry_run,
            fault=FaultInjector(fault), exclude=ws.excluded, acquire_lock=False, provenance=ws.config.provenance,
        )

    # ------------------------------------------------------------- inspection
    def check(self, root: str | Path | None = None) -> SourceChanges:
        """Compare the sources on disk (the configured source directory, or ``root``)
        with the last committed build."""
        src = Path(root).resolve() if root is not None else self.source_root
        sr, _ = changed_sources(self.store, src, self.workspace.excluded)
        reg = self.store.load_sources()
        return SourceChanges(
            root=str(src),
            new=list(sr.new),
            modified=[p for _, p in sr.modified],
            deleted=[reg[s].path for s in sr.deleted],
            renamed=[(o, n) for _, o, n in sr.renamed],
            unchanged=len(sr.unchanged),
            duplicates=[list(g) for g in sr.duplicates],
        )

    def status(self) -> Status:
        """HEAD, artifact counts, and what is stale relative to the sources on disk."""
        sr, changed = changed_sources(self.store, self.source_root, self.workspace.excluded)
        stale = stale_artifacts(self.store, changed) if changed else {}
        return Status(
            workspace=str(self.root),
            source_root=str(self.source_root),
            output_root=str(self.output_root),
            provider=self.workspace.config.provider,
            head=self.store.head,
            counts=self.store.counts(),
            up_to_date=not sr.has_changes,
            source_changes=sr.summary(),
            stale={k: len(v) for k, v in sorted(stale.items(), key=lambda kv: LAYER.get(kv[0], 9))},
            new_sources=len(sr.new),
        )

    def plan(self, *, full: bool = False, strategy: str | None = None, provider: str | None = None) -> BuildReport:
        """Dry run: what ``compile`` would rebuild and how many LLM calls it would make.
        Nothing is written and nothing is spent. Exact for free providers, an upper
        bound for LLM providers (``report.exact``)."""
        strat = strategy or ("full" if full else "dakc")
        report, _ = self._compiler(strat, dry_run=True, provider=provider, jobs=None).run()
        return report

    def artifacts(self, kind: str | None = None, *, query: str | None = None, limit: int | None = 100) -> list[ArtifactSummary]:
        """Current artifacts (and sources), optionally of one kind and/or matching
        ``query`` (a case-insensitive substring of the title; of the full text for
        claims and passages). Ordered by layer, then id."""
        if kind is not None and kind not in KINDS:
            raise InvalidArgument(f"kind must be one of {', '.join(KINDS)}")
        q = query.casefold() if query else None
        out: list[ArtifactSummary] = []
        for summary, haystack in self._iter_artifacts(kind, query):
            if q is None or q in haystack.casefold():
                out.append(summary)
                if limit is not None and len(out) >= limit:
                    break
        return out

    def _iter_artifacts(self, kind: str | None, query: str | None) -> Iterator[tuple[ArtifactSummary, str]]:
        kinds = [kind] if kind else sorted(KINDS, key=lambda k: LAYER[k])
        # SQLite LIKE folds case only for ASCII, so a non-ASCII query skips the pre-filter.
        like = f"%{query}%" if query and query.isascii() else "%"
        for k in kinds:
            if k == "source":
                rows = self.store.conn.execute(
                    "SELECT id, path FROM sources WHERE to_build IS NULL AND path LIKE ? ORDER BY id", (like,))
                for sid, path in rows:
                    yield ArtifactSummary(sid, "source", path), path
                continue
            # LIKE on the stored JSON is a cheap pre-filter; the title check above is exact.
            rows = self.store.conn.execute(
                "SELECT id FROM artifacts WHERE to_build IS NULL AND kind=? AND (? = '%' OR payload LIKE ? OR id LIKE ?) ORDER BY id",
                (k, like, like, like))
            batch: list[str] = []
            for (aid,) in rows:
                batch.append(aid)
                if len(batch) == 500:
                    yield from self._summaries(batch, k)
                    batch = []
            yield from self._summaries(batch, k)

    def _summaries(self, ids: list[str], kind: str) -> Iterator[tuple[ArtifactSummary, str]]:
        """(summary, text to match a query against): the title, or the full text of a passage."""
        if not ids:
            return
        names = titles(self.store, ids)
        full: dict[str, str] = {}
        if kind == "passage":
            q = f"SELECT id, json_extract(payload, '$.text') FROM artifacts WHERE to_build IS NULL AND id IN ({','.join('?' * len(ids))})"
            full = dict(self.store.conn.execute(q, ids).fetchall())
        for aid in ids:
            title = names.get(aid, aid)
            yield ArtifactSummary(aid, kind, title), full.get(aid, title)

    def read(self, artifact: str) -> ArtifactContent:
        """The current version of an artifact. Pages, summaries and the index return
        their rendered Markdown; claims and passages their text; a source document
        its current text on disk."""
        aid = self.resolve(artifact)
        if kind_of(aid) == "source":
            return self._read_source(aid)
        cur = self.store.current_artifact(aid)
        if cur is None:  # pragma: no cover - resolve() only returns current ids
            raise ArtifactNotFound(f"no current artifact {aid!r}")
        art, version, from_build = cur
        payload = art.payload()
        text = payload.get("rendered") or payload.get("text") or art.title
        meta = {k: v for k, v in payload.items() if k not in ("rendered", "text", "body")}
        if art.kind == "claim":
            meta = {
                "mentions": list(art.mentions),  # type: ignore[attr-defined]
                "supports": [{"passage_id": p, "text": t, "evidence_sentences": list(e)} for p, t, _, e in art.supports],  # type: ignore[attr-defined]
            }
        title = titles(self.store, [aid]).get(aid, art.title)
        return ArtifactContent(aid, art.kind, title, version, from_build, text, meta)

    def _read_source(self, aid: str) -> ArtifactContent:
        """A source document as it is on disk now (it may differ from the last build;
        ``metadata['changed_since_build']`` says so). Markdown, text and JSON are
        returned as written; HTML and PDF as the text DAKC extracts from them."""
        src = self.store.current_source(aid)
        assert src is not None
        row = self.store.conn.execute("SELECT from_build FROM sources WHERE id=? AND to_build IS NULL", (aid,)).fetchone()
        meta: dict[str, Any] = {"path": src.path, "type": src.type, "recorded_raw_hash": src.raw_hash}
        path = self.source_root / src.path
        text = ""
        if not path.is_file():
            meta["on_disk"] = "missing (deleted or moved since the last build)"
        else:
            meta["on_disk"] = "present"
            meta["changed_since_build"] = hash_file(path) != src.raw_hash
            raw = path.read_bytes()
            if src.type in ("markdown", "text", "json"):
                text = raw.decode("utf-8", errors="replace")
            else:
                try:
                    text = "\n\n".join(b.text for b in parse_bytes(raw, src.type).blocks)
                except ParseError as exc:
                    meta["parse_error"] = str(exc)
        return ArtifactContent(aid, "source", src.path, None, row[0] if row else None, text, meta)

    def explain(self, artifact: str) -> Explanation:
        """Dependencies, dependents, upstream sources and live staleness of one artifact."""
        aid = self._lookup(artifact) or artifact.strip()
        exp = explain(self.store, aid, self._changed())
        if exp.status == "UNKNOWN":
            raise ArtifactNotFound(self._not_found(artifact))
        return exp

    def trace(self, artifact: str) -> PageTrace | ClaimTrace:
        """Sentence-level provenance. For a page: each displayed sentence, the claims it
        cites, and each claim's passage, source version and evidence sentences. For a
        claim: its evidence."""
        aid = self.resolve(artifact)
        cur = self.store.current_artifact(aid)
        if cur is None or cur[0].kind not in ("page", "claim"):
            raise InvalidArgument(f"trace needs a page or a claim; {artifact!r} is a {kind_of(aid)}")
        art = cur[0]
        return trace_page(self.store, art) if art.kind == "page" else trace_claim(self.store, art)  # type: ignore[arg-type]

    def graph_stats(self) -> dict[str, dict[str, int]]:
        """Artifact counts per kind and dependency-edge counts per edge kind (HEAD)."""
        return stats(self.store)

    def downstream(self, artifact: str, *, max_depth: int | None = None) -> Downstream:
        """Everything that would be invalidated if ``artifact`` (or source) changed."""
        root = self.resolve(artifact)
        depth = downstream(self.store, root, max_depth)
        ids = [i for i in depth if i != root]
        names = titles(self.store, ids + [root])
        by_kind: dict[str, list[ArtifactSummary]] = {}
        for i in sorted(ids):
            by_kind.setdefault(kind_of(i), []).append(ArtifactSummary(i, kind_of(i), names.get(i, i)))
        ordered = dict(sorted(by_kind.items(), key=lambda kv: LAYER.get(kv[0], 9)))
        return Downstream(root, names.get(root, root), ordered)

    def export_graph(self, fmt: str = "json", *, around: str | None = None) -> str:
        """The dependency graph (or the part downstream of ``around``) as JSON
        (node-link), Graphviz DOT, or React Flow JSON."""
        if fmt not in GRAPH_FORMATS:
            raise InvalidArgument(f"format must be one of {', '.join(GRAPH_FORMATS)}")
        ids = None if around is None else set(downstream(self.store, self.resolve(around)))
        return export(build_graph(self.store, ids, with_titles=True), fmt)

    def builds(self, limit: int | None = 20) -> list[BuildRecord]:
        """Build history, newest first (an audit log: committed, failed, rolled back)."""
        return self.store.builds(limit)

    def diff(self, a: int, b: int | None = None) -> BuildDiff:
        """Artifacts created, updated or deleted between build ``a`` and build ``b`` (default HEAD)."""
        b = self.store.head if b is None else b
        for n in (a, b):
            if n != 0 and self.store.build(n) is None:
                raise InvalidArgument(f"no build {n}")
        d = self.store.diff_builds(a, b)
        ordered = dict(sorted(d.items(), key=lambda kv: LAYER.get(kv[0], 9)))
        return BuildDiff(a, b, {k: [tuple(x) for x in v] for k, v in ordered.items()})  # type: ignore[misc]

    def duplicates(self, limit: int | None = None) -> list[DuplicatePair]:
        """Claim pairs that are likely the same fact in different words. Advisory:
        DAKC never merges them (claim identity is exact)."""
        from dakc.claims import duplicate_candidates

        texts: dict[str, str] = {}
        for cid, payload in self.store.conn.execute("SELECT id, payload FROM artifacts WHERE kind='claim' AND to_build IS NULL"):
            supports = json.loads(payload)["supports"]
            texts[cid] = min(s[1] for s in supports) if supports else ""
        pairs = duplicate_candidates(list(texts.items()))
        return [DuplicatePair(a, b, texts[a], texts[b]) for a, b in pairs[:limit]]

    def verify(self, *, against_full: bool = False, allow_cost: bool = False) -> VerifyReport:
        """Check the store's invariants (edges, acyclicity, provenance, wiki files).
        With ``against_full``, also rebuild from scratch in a temporary store and
        require the result to equal the current state. With an LLM provider that
        re-pays every call, so it needs ``allow_cost=True``."""
        from dakc.provenance.verify import verify_against_full, verify_store

        res = verify_store(self.store, self.output_root, self.workspace.state_dir)
        out = VerifyReport(res.ok, res.checks, res.problems, res.metrics)
        if against_full:
            bundle = self.provider()
            if not bundle.free and not allow_cost:
                raise CostNotAllowed("a full-rebuild comparison with an LLM provider re-pays every call; pass allow_cost=True")
            ws = self.workspace
            equal, detail = verify_against_full(self.store, ws.source_root, bundle, ws.config.alias_map, ws.excluded,
                                                ws.config.provenance, ws.config.fingerprint)
            out.equal_to_full_rebuild = equal
            out.full_rebuild_diff = detail
            out.ok = out.ok and equal
        return out

    # --------------------------------------------------------------- mutations
    def compile(
        self,
        *,
        full: bool = False,
        strategy: str | None = None,
        provider: str | None = None,
        jobs: int | None = None,
        wait: float = 0.0,
        fault: str | None = None,
    ) -> BuildReport:
        """Compile: rebuild only what the source changes invalidated (``full=True``
        rebuilds everything), commit atomically, and update the wiki files.

        Holds the workspace build lock for the whole build; ``wait`` is how many
        seconds to wait for another build (:class:`~dakc.errors.BuildLocked` after).
        On failure nothing is committed and :class:`~dakc.errors.CompileFailed` is
        raised. ``fault`` injects a failure and is for tests only.
        """
        strat = strategy or ("full" if full else "dakc")
        comp = self._compiler(strat, dry_run=False, provider=provider, jobs=jobs, fault=fault)  # validates first
        with workspace_lock(self.workspace.db_path, wait):
            head_before = self.store.head
            try:
                report, _ = comp.run()
            except Exception as exc:  # provider, network, parse, injected faults
                head = self.store.head
                raise CompileFailed(f"{type(exc).__name__}: {exc}", head=head, preserved=head == head_before) from exc
            if report.status in ("committed", "noop") or not self.output_root.exists():
                report.projection = sync_projection(self.store, self.output_root, self.workspace.state_dir)
        return report

    def rollback(self, to: int | None = None, *, wait: float = 0.0) -> RollbackResult:
        """Restore the knowledge base (and the wiki) to an earlier committed build,
        recorded as a new build. Destructive: the artifact versions written by
        later builds are discarded (their build records stay in the audit log as
        ``rolled_back``; recompiling recreates the artifacts, reusing cached
        extraction results). Sources on disk are not touched. ``to`` defaults to
        the last committed build before HEAD."""
        with workspace_lock(self.workspace.db_path, wait):
            store = self.store
            if to is None:
                earlier = [b.id for b in store.builds() if b.status == "committed" and b.id < store.head]
                if not earlier:
                    raise RollbackError("no earlier committed build to roll back to")
                to = earlier[0]
            new_id = rollback(store, to)
            proj = sync_projection(store, self.output_root, self.workspace.state_dir)
        return RollbackResult(to, new_id, proj)
