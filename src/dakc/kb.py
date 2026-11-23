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
from collections import Counter
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dakc import versions
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
from dakc.graph import build_graph, downstream, export, resolve_id, stats, titles, upstream
from dakc.hashing import h
from dakc.parsing import ParseError, parse_bytes
from dakc.provenance import Explanation, changed_sources, explain, stale_artifacts
from dakc.provenance.trace import ClaimTrace, PageTrace, trace_claim, trace_page
from dakc.providers import ProviderBundle
from dakc.publishers import PublishResult, forget
from dakc.publishers import publish as publish_to
from dakc.sources import SourceSet
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
    # Why a compile has work even if no source changed: DAKC's own logic, the
    # configuration, or the provider changed since HEAD. Empty when there is none.
    outdated: list[str] = field(default_factory=list)
    conflicts_open: int = 0  # conflicts found at HEAD and not accepted in dakc.toml (RFC 0002)
    links_broken: int = 0  # links between documents that point at a missing document or heading

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BrokenLink:
    """A link from one document to another document, or heading, that no longer exists.

    ``kind`` is ``missing_document`` or ``missing_anchor``. ``suggestion`` is the link's new
    target when DAKC saw the rename that broke it (a moved file, a renamed heading), else empty."""

    id: str
    kind: str
    path: str
    line: int
    text: str
    href: str
    target: str
    anchor: str
    reason: str
    suggestion: str
    since_build: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConflictLocation:
    """One place that states a value: the claim, and where its source says it."""

    claim_id: str
    passage_id: str
    path: str
    heading: str
    sentence: str
    scope: str
    line: int | None = None  # 1-based line in the source as it is on disk now; None if not found
    evidence: str = ""  # the source's own sentence(s) behind the claim, when the extractor recorded them

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConflictValue:
    value: str
    locations: list[ConflictLocation]

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "locations": [loc.to_dict() for loc in self.locations]}


@dataclass
class Conflict:
    """A property the sources disagree on (RFC 0002). Advisory: DAKC never picks a side.

    ``kind`` is ``conflict`` (the rules found the same scope holding two values),
    ``possible`` (the optional verifier, a model, judged two statements contradictory) or
    ``scoped`` (the values differ only across time, release, environment or status). ``accepted`` is the
    reason given in ``dakc.toml`` when someone reviewed it and decided to keep it."""

    id: str
    kind: str
    property: str
    reason: str
    values: list[ConflictValue]
    since_build: int
    accepted: str | None = None
    affected: dict[str, list[ArtifactSummary]] = field(default_factory=dict)  # filled by KnowledgeBase.conflict()

    @property
    def claim_ids(self) -> list[str]:
        return sorted({loc.claim_id for v in self.values for loc in v.locations})

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "property": self.property, "reason": self.reason,
            "values": [v.to_dict() for v in self.values], "since_build": self.since_build,
            "accepted": self.accepted,
            "affected": {k: [a.to_dict() for a in v] for k, v in self.affected.items()},
        }


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
class Neighborhood:
    """The artifacts around one artifact, for drawing: what it is built from (``up``)
    and what is built from it (``down``), capped per kind, with the number hidden."""

    root: str
    nodes: list[dict[str, Any]] = field(default_factory=list)  # id, kind, title, layer, side, distance
    edges: list[dict[str, str]] = field(default_factory=list)  # dependency -> dependent, kind
    hidden: dict[str, int] = field(default_factory=dict)  # kind -> nodes not shown

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
class SourceInfo:
    """One configured source (RFC 0001)."""

    name: str
    type: str
    prefix: str
    location: str
    capabilities: list[str]
    documents: int  # sources at HEAD that came from it

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RollbackResult:
    restored_build: int
    new_build: int
    projection: dict[str, int]
    publications: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


#: DAKC logic versions a compile compares with HEAD (entity resolution is part of the configuration fingerprint).
LOGIC_LAYERS = {"parser": "parser", "claim_identity": "claim identity", "render": "rendering",
                "summary": "summary structure", "page": "page structure"}


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
        return changed_sources(self.store, self.workspace.source_set())[1]

    def _compiler(self, strategy: str, *, dry_run: bool, provider: str | None, jobs: int | None,
                  fault: str | None = None) -> Compiler:
        if strategy not in STRATEGIES:
            raise InvalidArgument(f"strategy must be one of {', '.join(STRATEGIES)}")
        ws = self.workspace
        return Compiler(
            self.store, self.provider(provider), ws.source_set(),
            aliases=ws.config.alias_map, config_fingerprint=ws.config.fingerprint,
            strategy=strategy, jobs=jobs or ws.config.jobs, dry_run=dry_run,
            fault=FaultInjector(fault), exclude=ws.excluded, acquire_lock=False, provenance=ws.config.provenance,
            conflict_rules=ws.config.conflict_rules, conflicts_accepted=set(ws.config.accepted_conflicts),
            conflict_verifier=ws.config.conflict_verifier,
        )

    # ------------------------------------------------------------- inspection
    def check(self, root: str | Path | None = None) -> SourceChanges:
        """Compare the sources on disk (the configured source directory, or ``root``)
        with the last committed build."""
        ss = SourceSet.from_root(Path(root).resolve(), self.workspace.excluded) if root is not None else self.workspace.source_set()
        sr, _ = changed_sources(self.store, ss)
        reg = self.store.load_sources()
        return SourceChanges(
            root=ss.describe(),
            new=list(sr.new),
            modified=[p for _, p in sr.modified],
            deleted=[reg[s].path for s in sr.deleted],
            renamed=[(o, n) for _, o, n in sr.renamed],
            unchanged=len(sr.unchanged),
            duplicates=[list(g) for g in sr.duplicates],
        )

    def status(self) -> Status:
        """HEAD, artifact counts, and what is stale relative to the sources on disk."""
        ss = self.workspace.source_set()
        sr, changed = changed_sources(self.store, ss)
        stale = stale_artifacts(self.store, changed) if changed else {}
        outdated = self._outdated()
        return Status(
            workspace=str(self.root),
            source_root=ss.describe(),
            output_root=str(self.output_root),
            provider=self.workspace.config.provider,
            head=self.store.head,
            counts=self.store.counts(),
            up_to_date=not sr.has_changes and not outdated,
            outdated=outdated,
            conflicts_open=sum(1 for c in self.conflicts()),
            links_broken=self.store.conn.execute("SELECT COUNT(*) FROM links WHERE to_build IS NULL").fetchone()[0],
            source_changes=sr.summary(),
            stale={k: len(v) for k, v in sorted(stale.items(), key=lambda kv: LAYER.get(kv[0], 9))},
            new_sources=len(sr.new),
        )

    def plan(self, *, full: bool = False, strategy: str | None = None, provider: str | None = None) -> BuildReport:
        """Dry run: what ``compile`` would rebuild and how many LLM calls it would make.
        Nothing is written and nothing is spent. Exact for free providers. For LLM
        providers the extraction calls are exact and the facts in new text are
        estimated with the heuristic extractor, so page and summary counts are an
        estimate (``report.exact`` is false)."""
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

    def _outdated(self) -> list[str]:
        """What a compile would redo besides source changes, read from what HEAD recorded:
        the versions of DAKC's own logic, the configuration fingerprint, and the
        provider's component fingerprints."""
        store = self.store
        counts = store.counts()
        if not store.head or not any(v for k, v in counts.items() if k != "edges"):
            return []
        out: list[str] = []
        stored = json.loads(store.get_meta("compiler_versions") or "{}")
        current = versions.current()
        if not stored:
            out.append("built by a DAKC version that predates logic versions: every layer is recomputed")
        else:
            for key, label in LOGIC_LAYERS.items():
                if stored.get(key) != current[key]:
                    out.append(f"{label} logic changed ({stored.get(key) or 'none'} -> {current[key]})")
        cfg = store.get_meta("config_fingerprint")
        if cfg and cfg != self.workspace.config.fingerprint:
            out.append("configuration changed (entity aliases, entity resolution or provenance mode)")
        from dakc.conflicts import fingerprint as conflicts_fingerprint

        from dakc.links import LINKS_VERSION

        lfp = store.get_meta("links_fp")
        if lfp is None:
            out.append("links between documents have not been checked on this knowledge base yet")
        elif lfp and lfp != LINKS_VERSION:
            out.append("link checking changed")
        cfp = store.get_meta("conflicts_fp")
        if cfp is None:
            out.append("conflict detection has not run on this knowledge base yet")
        elif cfp and cfp != conflicts_fingerprint(self.workspace.config.conflict_rules, self.workspace.config.alias_map):
            out.append("conflict detection, its [conflicts] scope rules or the aliases changed")
        build, seen = store.build(store.head), set()
        while build is not None and "fingerprints" not in build.stats and build.stats.get("restored_to") and build.id not in seen:
            seen.add(build.id)  # a rollback records no fingerprints: use the build it restored
            build = store.build(int(build.stats["restored_to"]))
        recorded = build.stats.get("fingerprints") if build is not None else None
        if recorded:
            try:
                now = self.provider().fingerprints()
            except Exception:  # e.g. an optional extra is missing; compile will say so
                now = {}
            for comp in ("extractor", "writer", "summarizer"):
                if now.get(comp) and recorded.get(comp) and recorded[comp] != now[comp]:
                    out.append(f"the {comp} changed ({recorded[comp]} -> {now[comp]})")
        out += self._verifier_outdated()
        return out

    def _verifier_outdated(self) -> list[str]:
        """What the conflict verifier still has to do (RFC 0002, phase 2)."""
        settings, stored = self.workspace.config.conflict_verifier, self.store.get_meta("verifier_fp") or ""
        if settings is None:
            return ["the conflict verifier was turned off: its findings are removed by the next compile"] if stored else []
        try:
            vf = getattr(self.provider().verifier, "fingerprint", "")
        except Exception:
            return []
        if not vf:
            return []
        if stored != vf:
            return ["the conflict verifier was turned on or changed: the next compile checks candidate pairs"]
        if self.store.get_meta("verifier_complete") != vf:
            return ["the conflict verifier has candidate pairs left to check (max_checks)"]
        return []

    def _read_source(self, aid: str) -> ArtifactContent:
        """A source document as it is on disk now (it may differ from the last build;
        ``metadata['changed_since_build']`` says so). Markdown, text and JSON are
        returned as written; HTML and PDF as the text DAKC extracts from them."""
        src = self.store.current_source(aid)
        assert src is not None
        row = self.store.conn.execute("SELECT from_build FROM sources WHERE id=? AND to_build IS NULL", (aid,)).fetchone()
        meta: dict[str, Any] = {"path": src.path, "type": src.type, "recorded_raw_hash": src.raw_hash}
        raw = self.workspace.source_set().read_current(src.path)
        text = ""
        if raw is None:
            meta["on_disk"] = "missing (deleted or moved since the last build)"
        else:
            meta["on_disk"] = "present"
            meta["changed_since_build"] = h(raw) != src.raw_hash
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

    # ------------------------------------------------------------- conflicts
    def links(self, path: str | None = None) -> list[BrokenLink]:
        """Broken links between documents at HEAD, in document and line order. With ``path``,
        only those in that document or pointing at it. Advisory: nothing is changed."""
        out = []
        for lid, payload, from_build in self.store.conn.execute(
                "SELECT id, payload, from_build FROM links WHERE to_build IS NULL"):
            d = json.loads(payload)
            if path and path not in (d["path"], d["target"]):
                continue
            out.append(BrokenLink(lid, d["kind"], d["path"], d["line"], d["text"], d["href"], d["target"], d["anchor"],
                                  d["reason"], d.get("suggestion", ""), from_build))
        return sorted(out, key=lambda b: (b.path, b.line, b.id))

    def linked_from(self, path: str) -> list[dict[str, Any]]:
        """Every link in another document that points at the document ``path`` (working or not):
        who would be affected by renaming or moving it, or its headings."""
        from dakc.links import DocLinks, resolve

        paths = {sid: p for sid, p in self.store.conn.execute("SELECT id, path FROM sources WHERE to_build IS NULL")}
        out = []
        for sid, links, anchors in self.store.conn.execute("SELECT source_id, links, anchors FROM link_index"):
            src = paths.get(sid)
            if src is None or src == path:
                continue
            for link in DocLinks.from_json(links, anchors).links:
                r = resolve(src, link.href)
                if r and path in r[0]:
                    out.append({"path": src, "line": link.line, "text": link.text, "href": link.href, "anchor": r[1]})
        return sorted(out, key=lambda d: (d["path"], d["line"]))

    def conflicts(self, *, scoped: bool = False, accepted: bool = False) -> list[Conflict]:
        """Properties the sources disagree on, at HEAD (RFC 0002). By default only open
        conflicts: ``scoped=True`` adds differences explained by scope, ``accepted=True``
        adds conflicts accepted in ``dakc.toml``. Nothing here changes a claim or a page."""
        acc = self.workspace.config.accepted_conflicts
        rows = self.store.conn.execute(
            "SELECT id, kind, payload, from_build FROM conflicts WHERE to_build IS NULL ORDER BY id").fetchall()
        out = []
        for cid, kind, payload, from_build in rows:
            if (kind == "scoped" and not scoped) or (cid in acc and not accepted):
                continue
            out.append(self._conflict(cid, kind, json.loads(payload), from_build, acc.get(cid)))
        rank = {"conflict": 0, "possible": 1, "scoped": 2}
        return sorted(out, key=lambda c: (c.accepted is not None, rank.get(c.kind, 2), c.property, c.id))

    def conflict(self, ident: str) -> Conflict:
        """One finding, with the line each value is stated on and the pages and summaries
        built from the claims involved. ``ident`` is its id (``cfl:…``), a unique prefix
        of it, or the same with ``conflict:`` in place of ``cfl:``."""
        key = ident.strip()
        if key.startswith("conflict:"):
            key = "cfl:" + key[len("conflict:"):]
        if not key.startswith("cfl:"):
            key = "cfl:" + key
        rows = self.store.conn.execute(
            "SELECT id, kind, payload, from_build FROM conflicts WHERE to_build IS NULL AND id LIKE ? ORDER BY id",
            (key.replace("%", "").replace("_", "") + "%",)).fetchall()
        if not rows:
            raise ArtifactNotFound(f"no conflict {ident!r} at HEAD (`dakc conflicts --all` lists them)")
        if len(rows) > 1:
            raise InvalidArgument(f"{ident!r} matches {len(rows)} conflicts: " + ", ".join(r[0] for r in rows[:5]))
        cid, kind, payload, from_build = rows[0]
        c = self._conflict(cid, kind, json.loads(payload), from_build, self.workspace.config.accepted_conflicts.get(cid))
        ss, texts = self.workspace.source_set(), {}
        for v in c.values:
            for loc in v.locations:
                if loc.path not in texts:
                    raw = ss.read_current(loc.path)
                    texts[loc.path] = raw.decode("utf-8", errors="replace") if raw is not None else None
                loc.evidence = self._evidence_of(loc)
                loc.line = self._line_of(texts[loc.path], loc)
        affected: set[str] = set()
        for claim in c.claim_ids:
            affected |= {i for i in downstream(self.store, claim) if kind_of(i) in ("page", "summary")}
        names = titles(self.store, sorted(affected))
        by_kind: dict[str, list[ArtifactSummary]] = {}
        for i in sorted(affected):
            by_kind.setdefault(kind_of(i), []).append(ArtifactSummary(i, kind_of(i), names.get(i, i)))
        c.affected = dict(sorted(by_kind.items(), key=lambda kv: LAYER.get(kv[0], 9)))
        return c

    def _conflict(self, cid: str, kind: str, payload: dict[str, Any], from_build: int, accepted: str | None) -> Conflict:
        # since: the start of the unbroken run of versions of this kind that ends at HEAD
        since = from_build
        for f, t, k in self.store.conn.execute(
                "SELECT from_build, to_build, kind FROM conflicts WHERE id=? AND to_build IS NOT NULL ORDER BY to_build DESC", (cid,)):
            if t != since or k != kind:
                break
            since = f
        values = [ConflictValue(v["value"], [ConflictLocation(o["claim_id"], o["passage_id"], o["path"], o["heading"],
                                                              o["sentence"], o["scope"]) for o in v["occurrences"]])
                  for v in payload["values"]]
        return Conflict(cid, kind, payload["property"], payload["reason"], values, since, accepted)

    def _evidence_of(self, loc: ConflictLocation) -> str:
        from dakc.claims import split_sentences

        cur, psg = self.store.current_artifact(loc.claim_id), self.store.current_artifact(loc.passage_id)
        if cur is None or psg is None:
            return ""
        sents = split_sentences(psg[0].text)  # type: ignore[attr-defined]
        for pid, _t, _m, ev in cur[0].supports:  # type: ignore[attr-defined]
            if pid == loc.passage_id:
                return " ".join(sents[i] for i in ev if 0 <= i < len(sents))
        return ""

    def _line_of(self, text: str | None, loc: ConflictLocation) -> int | None:
        """The line a claim's evidence starts on, in the source as it is now."""
        if not text:
            return None
        needles = [loc.evidence, loc.sentence]
        psg = self.store.current_artifact(loc.passage_id)
        if psg is not None and psg[0].text.strip():  # type: ignore[attr-defined]
            needles.append(psg[0].text.strip().splitlines()[0])  # type: ignore[attr-defined]
        flat = re.sub(r"\s+", " ", text)
        for needle in needles:
            n = re.sub(r"\s+", " ", needle).strip()
            if len(n) < 8:
                continue
            for probe in (n, n[:60]):
                i = text.find(probe)
                if i >= 0:
                    return text.count("\n", 0, i) + 1
                j = flat.find(probe)
                if j >= 0:  # the sentence wraps across lines: map back through the collapsed text
                    return _line_at_flat_offset(text, j)
        return None

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

    def neighborhood(self, artifact: str, *, up: int = 2, down: int = 2, per_kind: int = 8) -> Neighborhood:
        """The dependency graph around ``artifact``, up to ``up`` steps towards the sources
        and ``down`` steps towards the index, with at most ``per_kind`` nodes of each kind.
        Nodes are chosen outwards from the artifact, so every shown node is connected to it."""
        root = self.resolve(artifact)
        side: dict[str, tuple[str, int]] = {root: ("root", 0)}
        for i, d in upstream(self.store, root, up).items():
            side.setdefault(i, ("up", d))
        for i, d in downstream(self.store, root, down).items():
            side.setdefault(i, ("down", d))
        edges = [(a, b, k) for a, b, k in self.store.dependencies_many(list(side)) if a in side and b in side]
        towards = {"up": {}, "down": {}}  # direction -> node -> neighbours further out
        for dependent, dependency, _ in edges:
            towards["up"].setdefault(dependent, []).append(dependency)
            towards["down"].setdefault(dependency, []).append(dependent)
        kept, shown = {root}, Counter()
        for direction in ("up", "down"):
            frontier = [root]
            while frontier:
                nxt = []
                for node in sorted(frontier):
                    for other in sorted(towards[direction].get(node, ())):
                        if other in kept or side[other][0] != direction:
                            continue
                        kind = kind_of(other)
                        if shown[kind] < per_kind:
                            kept.add(other)
                            shown[kind] += 1
                            nxt.append(other)
                frontier = nxt
        total = Counter(kind_of(i) for i in side if i != root)
        names = titles(self.store, sorted(kept))
        nodes = [{"id": i, "kind": kind_of(i), "title": names.get(i, i), "layer": LAYER.get(kind_of(i), 9),
                  "side": side[i][0], "distance": side[i][1]} for i in sorted(kept, key=lambda i: (LAYER.get(kind_of(i), 9), i))]
        return Neighborhood(
            root,
            nodes,
            [{"dependency": b, "dependent": a, "kind": k} for a, b, k in edges if a in kept and b in kept],
            {k: n - shown[k] for k, n in total.items() if n > shown[k]},
        )

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

        res = verify_store(self.store, self.output_root, self.workspace.state_dir, self.workspace.config.conflict_rules,
                           self.workspace.config.alias_map)
        out = VerifyReport(res.ok, res.checks, res.problems, res.metrics)
        if against_full:
            bundle = self.provider()
            if not bundle.free and not allow_cost:
                raise CostNotAllowed("a full-rebuild comparison with an LLM provider re-pays every call; pass allow_cost=True")
            ws = self.workspace
            equal, detail = verify_against_full(self.store, ws.source_set(), bundle, ws.config.alias_map, ws.excluded,
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
        publishers = self.workspace.publishers()  # configuration errors surface before anything is built
        with workspace_lock(self.workspace.db_path, wait):
            head_before = self.store.head
            try:
                report, _ = comp.run()
            except Exception as exc:  # provider, network, parse, injected faults
                head = self.store.head
                raise CompileFailed(f"{type(exc).__name__}: {exc}", head=head, preserved=head == head_before) from exc
            if report.status in ("committed", "noop") or not self.output_root.exists():
                report.projection = sync_projection(self.store, self.output_root, self.workspace.state_dir)
            if report.status in ("committed", "noop"):
                report.publications = [publish_to(self.store, n, pub).to_dict() for n, pub in publishers]
        return report

    def publish(self, name: str | None = None, *, dry_run: bool = False, reset: bool = False,
                wait: float = 0.0) -> list[PublishResult]:
        """Bring configured publishers (all, or the one called ``name``) up to date with
        HEAD, sending only what their ledger says they have not acknowledged. A
        failing publisher does not raise: its result carries the error, and the
        unsent work stays pending for the next run. ``reset`` forgets the ledger
        first, so everything is sent again. ``dry_run`` only counts pending work."""
        pubs = self.workspace.publishers()
        if name is not None:
            pubs = [(n, p) for n, p in pubs if n == name]
            if not pubs:
                raise InvalidArgument(f"no publisher named {name!r}; configured: "
                                      f"{', '.join(n for n, _ in self.workspace.publishers()) or 'none'}")
        with workspace_lock(self.workspace.db_path, wait):
            out = []
            for n, pub in pubs:
                if reset and not dry_run:
                    forget(self.store, n)
                out.append(publish_to(self.store, n, pub, dry_run=dry_run))
        return out

    def sources(self) -> list[SourceInfo]:
        """The configured sources, with how many documents each contributed at HEAD."""
        ss = self.workspace.source_set()
        counts: Counter[str] = Counter()
        for (path,) in self.store.conn.execute("SELECT path FROM sources WHERE to_build IS NULL"):
            o = ss.owner(path)
            counts[o.name if o else ""] += 1
        return [SourceInfo(s.name, s.connector.kind, s.prefix, s.describe(), sorted(s.connector.capabilities), counts[s.name])
                for s in ss.sources]

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
            pubs = [publish_to(store, n, pub).to_dict() for n, pub in self.workspace.publishers()]
        return RollbackResult(to, new_id, proj, pubs)


def _line_at_flat_offset(text: str, offset: int) -> int:
    """Line of ``text`` holding the character at ``offset`` in its whitespace-collapsed form."""
    pos, line, prev_space = 0, 1, False
    for ch in text:
        if ch.isspace():
            if not prev_space:
                pos += 1
            prev_space = True
        else:
            pos += 1
            prev_space = False
        if pos > offset:
            return line
        if ch == "\n":
            line += 1
    return line
