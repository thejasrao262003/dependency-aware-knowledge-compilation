"""Sources: where content comes from (RFC 0001, ``docs/rfcs/0001-sources-and-publishers.md``).

A *source connector* lists versioned *items* and reads them. A workspace has
one or more configured sources; each has a name and a path *prefix*, so its
items appear in the workspace under ``prefix/path``. :class:`SourceSet` scans
all of them against the source registry and produces the same
:class:`~dakc.ingestion.ScanResult` the compiler has always consumed, so the
compiler never learns which connector supplied a document.

Classification (per item, after every source has been listed):

* matched to a registered source by native key (connectors with ``native_ids``)
  or by path, bytes equal         -> unchanged
* matched, bytes differ           -> modified (and renamed, if a key match moved)
* unmatched, bytes equal to a registered source of the same connector that
  vanished                        -> renamed (source id kept)
* unmatched otherwise             -> new
* registered and not matched      -> deleted

A source that cannot be listed raises :class:`~dakc.errors.SourceError`: the
build fails before anything is committed, because a partial listing would look
like deletions.
"""

from __future__ import annotations

import importlib
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from dakc.errors import PublishError, SourceError
from dakc.hashing import h, hash_file
from dakc.ingestion import FileInfo, ScanResult, SourceRecord, discover
from dakc.parsing import source_type_for

#: Capabilities a connector may declare (RFC 0001). Others are reserved.
NATIVE_IDS = "native_ids"  # items carry a stable ``key``; matching is by key, so a move plus an edit is a rename
CONTENT_HASH = "content_hash"  # ``revision`` is a hash of the bytes; an unchanged revision is never read


@dataclass(frozen=True)
class Item:
    """One versioned content object a connector provides (a file, a document, a record)."""

    path: str  # where it is, relative to the source, POSIX style; may change
    source_type: str  # how DAKC parses it: markdown, html, pdf, text, json
    revision: str  # opaque version marker; a content hash when the connector declares CONTENT_HASH
    size: int = 0
    key: str | None = None  # stable native id, when the connector declares NATIVE_IDS
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)


@runtime_checkable
class SourceConnector(Protocol):
    """What a source plugin implements. ``list`` returns a complete listing every time;
    ``read`` returns the bytes of exactly the given revision, or raises."""

    kind: str
    capabilities: frozenset[str]

    def list(self) -> Iterable[Item]: ...

    def read(self, item: Item) -> bytes: ...


class FilesystemSource:
    """A directory tree. Paths are relative to ``root``; revisions are content hashes."""

    kind = "filesystem"
    capabilities = frozenset({CONTENT_HASH})

    def __init__(self, root: Path, exclude: tuple[Path, ...] = ()):
        self.root = Path(root).resolve()
        self.exclude = exclude

    def list(self) -> Iterable[Item]:
        if not self.root.is_dir():  # an empty source; SourceSet.scan refuses if it held registered sources
            return
        for p in discover(self.root, self.exclude):
            yield Item(p.relative_to(self.root).as_posix(), source_type_for(p) or "text", hash_file(p), p.stat().st_size)

    def read(self, item: Item) -> bytes:
        return (self.root / item.path).read_bytes()

    def abspath(self, path: str) -> Path:
        return self.root / path

    def describe(self) -> str:
        return str(self.root)


@dataclass
class Source:
    """A configured source: a connector, its name, and the prefix of its paths."""

    name: str
    connector: SourceConnector
    prefix: str = ""
    options: dict[str, Any] = field(default_factory=dict)

    def full_path(self, path: str) -> str:
        return f"{self.prefix}/{path}" if self.prefix else path

    def local_path(self, full: str) -> str:
        return full[len(self.prefix) + 1:] if self.prefix else full

    def describe(self) -> str:
        d = getattr(self.connector, "describe", None)
        return d() if callable(d) else self.connector.kind


class SourceSet:
    """All sources of a workspace, scanned together into one namespace."""

    def __init__(self, sources: list[Source]):
        if not sources:
            raise SourceError("a workspace needs at least one source")
        names = [s.name for s in sources]
        if len(set(names)) != len(names):
            raise SourceError(f"source names must be unique: {names}")
        empty = [s.name for s in sources if not s.prefix]
        if len(empty) > 1:
            raise SourceError(f"at most one source may have an empty prefix: {empty}")
        prefixes = [s.prefix for s in sources if s.prefix]
        if len(set(prefixes)) != len(prefixes):
            raise SourceError(f"source prefixes must be unique: {prefixes}")
        for s in sources:
            if s.prefix and ("/" in s.prefix or s.prefix in (".", "..") or s.prefix.startswith(".")):
                raise SourceError(f"source {s.name!r}: prefix {s.prefix!r} must be a single path segment")
            if not isinstance(s.connector, SourceConnector):
                raise SourceError(f"source {s.name!r}: connector {type(s.connector).__name__} lacks kind, capabilities, list() or read()")
        self.sources = sources

    @classmethod
    def from_root(cls, root: Path, exclude: tuple[Path, ...] = ()) -> SourceSet:
        """The single-directory workspace every DAKC version before 0.5 had."""
        return cls([Source(Path(root).name or "knowledge", FilesystemSource(root, exclude))])

    # ---------------------------------------------------------------- lookup
    def owner(self, path: str) -> Source | None:
        """The source a workspace path belongs to (by prefix), or None."""
        head = path.split("/", 1)[0]
        for s in self.sources:
            if s.prefix and s.prefix == head and "/" in path:
                return s
        return next((s for s in self.sources if not s.prefix), None)

    def filesystem_roots(self) -> list[Path]:
        return [s.connector.root for s in self.sources if isinstance(s.connector, FilesystemSource)]

    def describe(self) -> str:
        if len(self.sources) == 1 and not self.sources[0].prefix:
            return self.sources[0].describe()
        return ", ".join(f"{s.name} ({s.connector.kind}: {s.describe()})" for s in self.sources)

    def read_current(self, path: str) -> bytes | None:
        """The current bytes of a workspace path, or None if no source lists it now."""
        src = self.owner(path)
        if src is None:
            return None
        local = src.local_path(path)
        if isinstance(src.connector, FilesystemSource):
            p = src.connector.abspath(local)
            return p.read_bytes() if p.is_file() else None
        for it in self._list(src):
            if it.path == local:
                return src.connector.read(it)
        return None

    # ------------------------------------------------------------------ scan
    def _list(self, src: Source) -> list[Item]:
        try:
            return list(src.connector.list())
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"source {src.name!r} ({src.connector.kind}) could not be listed: {type(exc).__name__}: {exc}") from exc

    def scan(self, registry: dict[str, SourceRecord], *, detect_renames: bool = True) -> ScanResult:
        result = ScanResult()
        prefixes = {s.prefix for s in self.sources if s.prefix}
        listed: list[tuple[Source, Item, str]] = []
        owner_of = {rec.id: self.owner(rec.path) for rec in registry.values()}
        for src in self.sources:
            conn = src.connector
            if isinstance(conn, FilesystemSource) and not conn.root.is_dir() and any(o is src for o in owner_of.values()):
                raise SourceError(
                    f"source {src.name!r}: directory {conn.root} is missing, but the knowledge base has sources from it. "
                    "Nothing was changed. To delete all of them, create the directory empty and compile."
                )
            for it in self._list(src):
                full = src.full_path(it.path)
                if not src.prefix and "/" in full and full.split("/", 1)[0] in prefixes:
                    other = next(s.name for s in self.sources if s.prefix == full.split("/", 1)[0])
                    raise SourceError(
                        f"{full!r} from source {src.name!r} is inside the prefix of source {other!r}; "
                        f"rename that directory or give source {other!r} another prefix"
                    )
                if full in result.files:
                    raise SourceError(f"two items of source {src.name!r} have the same path {full!r}")
                listed.append((src, it, full))
                result.files[full] = None  # type: ignore[assignment]  # reserve; filled below

        by_path = {rec.path: rec for rec in registry.values()}
        by_key: dict[tuple[str, str], SourceRecord] = {}
        for rec in registry.values():
            o = owner_of[rec.id]
            if rec.key is not None and o is not None:
                by_key[(o.name, rec.key)] = rec

        matched: dict[str, SourceRecord] = {}  # full path -> registered record it continues
        used: set[str] = set()
        for src, it, full in listed:
            rec = None
            if detect_renames and NATIVE_IDS in src.connector.capabilities and it.key is not None:
                rec = by_key.get((src.name, it.key))
            if rec is None:
                cand = by_path.get(full)
                if cand is not None and (cand.key is None or cand.key == it.key or not detect_renames):
                    rec = cand
            if rec is not None and rec.id not in used:
                matched[full] = rec
                used.add(rec.id)
            result.files[full] = self._info(src, it, full, matched.get(full))

        for src, it, full in listed:
            rec = matched.get(full)
            if rec is None:
                continue
            info = result.files[full]
            if rec.path != full:
                result.renamed.append((rec.id, rec.path, full))
            if rec.raw_hash != info.raw_hash:
                result.modified.append((rec.id, full))
            elif rec.path == full:
                if info.revision is not None and rec.revision != info.revision:
                    result.revised.append((rec.id, full))
                result.unchanged.append(rec.id)
            elif info.revision is not None and rec.revision != info.revision:
                result.revised.append((rec.id, full))

        vanished = [rec for rec in registry.values() if rec.id not in used]
        unknown = sorted(full for _, _, full in listed if full not in matched)
        if detect_renames:
            by_hash: dict[tuple[str | None, str], list[SourceRecord]] = defaultdict(list)
            for rec in sorted(vanished, key=lambda r: r.path):
                o = owner_of[rec.id]
                by_hash[(o.name if o else None, rec.raw_hash)].append(rec)
            still: list[str] = []
            for full in unknown:
                info = result.files[full]
                cands = by_hash.get((info.origin, info.raw_hash)) or by_hash.get((None, info.raw_hash))
                if cands:
                    rec = cands.pop(0)
                    result.renamed.append((rec.id, rec.path, full))
                    used.add(rec.id)
                    if info.revision is not None and rec.revision != info.revision:
                        result.revised.append((rec.id, full))
                else:
                    still.append(full)
            unknown = still
            vanished = [rec for rec in vanished if rec.id not in used]

        result.renamed.sort(key=lambda r: r[2])
        result.modified.sort(key=lambda m: m[1])
        result.new = sorted(unknown)
        result.deleted = sorted(rec.id for rec in vanished)
        groups: dict[str, list[str]] = defaultdict(list)
        for full, info in result.files.items():
            groups[info.raw_hash].append(full)
        result.duplicates = sorted(sorted(g) for g in groups.values() if len(g) > 1)
        return result

    def _info(self, src: Source, it: Item, full: str, rec: SourceRecord | None) -> FileInfo:
        conn = src.connector
        content_hash = CONTENT_HASH in conn.capabilities
        abspath = conn.abspath(it.path) if isinstance(conn, FilesystemSource) else None
        revision = None if content_hash else it.revision
        if content_hash:
            raw_hash = it.revision
            reader = None if abspath is not None else (lambda: conn.read(it))
        elif rec is not None and rec.revision is not None and rec.revision == it.revision:
            raw_hash, reader = rec.raw_hash, (lambda: conn.read(it))  # same revision: not read now
        else:
            data = conn.read(it)  # revision changed or unknown: read once to learn whether the bytes did
            raw_hash, reader = h(data), (lambda: data)
        return FileInfo(full, abspath, raw_hash, it.size, it.source_type, it.key, revision, src.name, reader)


# ------------------------------------------------------------------ plugins
SOURCE_GROUP = "dakc.sources"
PUBLISHER_GROUP = "dakc.publishers"


def _entry_points(group: str) -> dict[str, Any]:
    from importlib.metadata import entry_points

    return {ep.name: ep for ep in entry_points(group=group)}


def load_factory(group: str, type_: str, options: dict[str, Any], root: Path, builtins: dict[str, Callable]) -> Callable:
    """Find the factory for a source or publisher ``type``: a built-in, an installed
    entry point in ``group``, or ``custom`` with ``factory = "module:attr"`` and an
    optional ``plugin_path`` (relative to the workspace) to import it from."""
    what = "source" if group == SOURCE_GROUP else "publisher"
    err = SourceError if group == SOURCE_GROUP else PublishError
    if type_ == "custom":
        spec = options.get("factory")
        if not spec or ":" not in spec:
            raise err(f'a custom {what} needs factory = "module.path:callable"')
        if options.get("plugin_path"):
            p = str((root / options["plugin_path"]).resolve())
            if p not in sys.path:
                sys.path.insert(0, p)
        module_name, attr = spec.split(":", 1)
        try:
            return getattr(importlib.import_module(module_name), attr)
        except Exception as exc:
            raise err(f"cannot load custom {what} factory {spec!r}: {type(exc).__name__}: {exc}") from exc
    if type_ in builtins:
        return builtins[type_]
    eps = _entry_points(group)
    if type_ in eps:
        try:
            return eps[type_].load()
        except Exception as exc:
            raise err(f"cannot load {what} plugin {type_!r}: {type(exc).__name__}: {exc}") from exc
    known = sorted(set(builtins) | set(eps) | {"custom"})
    raise err(f"unknown {what} type {type_!r}; available: {', '.join(known)}")


def _make_filesystem(name: str, options: dict[str, Any], root: Path) -> FilesystemSource:
    if "path" not in options:
        raise SourceError(f"filesystem source {name!r} needs path = \"...\"")
    return FilesystemSource((root / options["path"]).resolve(), tuple(options.get("_exclude", ())))


SOURCE_BUILTINS: dict[str, Callable] = {"filesystem": _make_filesystem}

_RESERVED = {"name", "type", "prefix", "factory", "plugin_path"}


def make_source(spec: dict[str, Any], root: Path, exclude: tuple[Path, ...] = ()) -> Source:
    """Build a :class:`Source` from one ``[[sources]]`` table."""
    name = spec.get("name")
    if not name or not isinstance(name, str):
        raise SourceError("every [[sources]] entry needs a name")
    type_ = spec.get("type", "filesystem")
    options = {k: v for k, v in spec.items() if k not in {"name", "type", "prefix"}}
    if type_ == "filesystem":
        options["_exclude"] = exclude
    factory = load_factory(SOURCE_GROUP, type_, options, root, SOURCE_BUILTINS)
    kwargs = {k: v for k, v in options.items() if k not in _RESERVED and not k.startswith("_")} if type_ == "custom" else options
    try:
        connector = factory(name, kwargs, root)
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError(f"source {name!r} ({type_}) could not be created: {type(exc).__name__}: {exc}") from exc
    prefix = spec.get("prefix", name)
    return Source(name, connector, prefix, options)
