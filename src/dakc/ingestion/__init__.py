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
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from dakc.hashing import hash_file, slugify
from dakc.parsing import source_type_for


@dataclass(frozen=True)
class SourceRecord:
    id: str
    path: str
    raw_hash: str
    text_hash: str
    size: int
    type: str

    def payload(self) -> dict:
        return {"path": self.path, "raw_hash": self.raw_hash, "text_hash": self.text_hash, "size": self.size, "type": self.type}


@dataclass(frozen=True)
class FileInfo:
    path: str  # POSIX path relative to the source root
    abspath: Path
    raw_hash: str
    size: int
    source_type: str


@dataclass
class ScanResult:
    files: dict[str, FileInfo] = field(default_factory=dict)
    new: list[str] = field(default_factory=list)  # paths
    modified: list[tuple[str, str]] = field(default_factory=list)  # (source id, path)
    deleted: list[str] = field(default_factory=list)  # source ids
    renamed: list[tuple[str, str, str]] = field(default_factory=list)  # (source id, old, new)
    unchanged: list[str] = field(default_factory=list)  # source ids
    duplicates: list[list[str]] = field(default_factory=list)  # groups of paths with equal bytes

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
    root = root.resolve()
    result = ScanResult()
    for p in discover(root, exclude):
        rel = p.relative_to(root).as_posix()
        result.files[rel] = FileInfo(rel, p, hash_file(p), p.stat().st_size, source_type_for(p) or "text")

    by_path = {rec.path: rec for rec in registry.values()}
    vanished = [rec for rec in registry.values() if rec.path not in result.files]
    unknown = [path for path in result.files if path not in by_path]

    for path, info in result.files.items():
        rec = by_path.get(path)
        if rec is None:
            continue
        if rec.raw_hash == info.raw_hash:
            result.unchanged.append(rec.id)
        else:
            result.modified.append((rec.id, path))

    if detect_renames:
        vanished_by_hash: dict[str, list[SourceRecord]] = defaultdict(list)
        for rec in sorted(vanished, key=lambda r: r.path):
            vanished_by_hash[rec.raw_hash].append(rec)
        still_unknown: list[str] = []
        for path in sorted(unknown):
            candidates = vanished_by_hash.get(result.files[path].raw_hash)
            if candidates:
                rec = candidates.pop(0)
                result.renamed.append((rec.id, rec.path, path))
            else:
                still_unknown.append(path)
        renamed_ids = {r[0] for r in result.renamed}
        vanished = [rec for rec in vanished if rec.id not in renamed_ids]
        unknown = still_unknown

    result.new = sorted(unknown)
    result.deleted = sorted(rec.id for rec in vanished)

    by_hash: dict[str, list[str]] = defaultdict(list)
    for path, info in result.files.items():
        by_hash[info.raw_hash].append(path)
    result.duplicates = sorted(sorted(group) for group in by_hash.values() if len(group) > 1)
    return result


def new_source_id(path: str, taken: set[str]) -> str:
    """Readable, stable id derived from the first path a source was seen at."""
    base = "src:" + slugify(path)
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    taken.add(candidate)
    return candidate
