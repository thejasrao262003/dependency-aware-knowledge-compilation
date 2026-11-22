"""Source discovery and change classification.

A scan hashes the raw bytes of every supported file under the source root and
compares them with the source registry (the current ``sources`` rows):

* path known, hash equal          -> unchanged
* path known, hash differs        -> modified
* path unknown, hash equals that of a registered path that disappeared
                                  -> renamed (source id is kept)
* path unknown otherwise          -> new
* registered path missing         -> deleted (unless explained by a rename)

Modification times are never used to decide anything.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from dakc.hashing import slugify
from dakc.parsing import source_type_for


@dataclass(frozen=True)
class SourceRecord:
    id: str
    path: str
    raw_hash: str
    text_hash: str
    size: int
    type: str
    key: str | None = None  # the connector's native id, if it has stable ones (RFC 0001)
    revision: str | None = None  # the connector's version marker, when it is not a content hash

    def payload(self) -> dict:
        out = {"path": self.path, "raw_hash": self.raw_hash, "text_hash": self.text_hash, "size": self.size, "type": self.type}
        if self.key is not None:
            out["key"] = self.key
        if self.revision is not None:
            out["revision"] = self.revision
        return out


@dataclass(frozen=True)
class FileInfo:
    """One listed source item, as the compiler sees it: a path in the workspace's
    namespace, its content hash, and how to read it."""

    path: str  # POSIX path in the workspace namespace (source prefix + path within the source)
    abspath: Path | None
    raw_hash: str
    size: int
    source_type: str
    key: str | None = None
    revision: str | None = None
    origin: str = ""  # name of the source that listed it
    reader: Callable[[], bytes] | None = field(default=None, compare=False, repr=False)

    def read(self) -> bytes:
        if self.reader is not None:
            return self.reader()
        assert self.abspath is not None
        return self.abspath.read_bytes()


@dataclass
class ScanResult:
    files: dict[str, FileInfo] = field(default_factory=dict)
    new: list[str] = field(default_factory=list)  # paths
    modified: list[tuple[str, str]] = field(default_factory=list)  # (source id, path)
    deleted: list[str] = field(default_factory=list)  # source ids
    renamed: list[tuple[str, str, str]] = field(default_factory=list)  # (source id, old, new)
    unchanged: list[str] = field(default_factory=list)  # source ids
    duplicates: list[list[str]] = field(default_factory=list)  # groups of paths with equal bytes
    revised: list[tuple[str, str]] = field(default_factory=list)  # (source id, path): new revision, same bytes

    @property
    def has_changes(self) -> bool:
        return bool(self.new or self.modified or self.deleted or self.renamed)

    def summary(self) -> dict[str, int]:
        return {
            "new": len(self.new),
            "modified": len(self.modified),
            "deleted": len(self.deleted),
            "renamed": len(self.renamed),
            "unchanged": len(self.unchanged),
        }


def discover(root: Path, exclude: tuple[Path, ...] = ()) -> list[Path]:
    """All supported files under ``root`` in sorted order, skipping hidden dirs."""
    root = root.resolve()
    excluded = {p.resolve() for p in exclude}
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and (current / d).resolve() not in excluded
        )
        for name in filenames:
            if name.startswith("."):
                continue
            p = current / name
            if source_type_for(p) is not None:
                found.append(p)
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def scan(root: Path, registry: dict[str, SourceRecord], *, detect_renames: bool = True, exclude: tuple[Path, ...] = ()) -> ScanResult:
    """Scan one directory (the single-source case of :meth:`dakc.sources.SourceSet.scan`)."""
    from dakc.sources import SourceSet

    return SourceSet.from_root(root, exclude).scan(registry, detect_renames=detect_renames)


def new_source_id(path: str, taken: set[str]) -> str:
    """Readable, stable id derived from the first path a source was seen at."""
    base = "src:" + slugify(path)
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    taken.add(candidate)
    return candidate
