"""Project pages, summaries and the index from the store to Markdown files.

The projection is a pure function of the store's current state, so it can be
re-run at any time (after a crash, a rollback, a failed build, or manual
tampering). Each run also *repairs* the wiki: a file that was edited or deleted
by hand although its artifact did not change is rewritten from the store.

Ownership: the projection manifest (``.dakc/projection.json``) lists every file
the projection wrote. Only those files are ever rewritten, repaired or deleted,
so user files in the output directory are left alone.

Cost: repair must not read a large wiki on every compile. The manifest records
each file's size and ``mtime_ns`` when DAKC last wrote or checked it, and a file
is read and hashed only when its stat differs (as git and make decide what to
look at). A recorded stat is trusted only when its mtime is older than the
manifest itself: on a file system with coarse timestamps, an edit made in the
same tick as DAKC's write keeps the recorded mtime, so such "racily clean"
files are hashed once more on the next run (git's racy-index rule).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from dakc.hashing import h
from dakc.storage import Store

MANIFEST = "projection.json"

Stat = tuple[int, int]  # (size, mtime_ns)


def rel_path(artifact_id: str, kind: str) -> str:
    key = artifact_id.split(":", 1)[1]
    if kind == "page":
        return f"pages/{key}.md"
    if kind == "summary":
        return f"summaries/{key}.md"
    return "index.md"


def _atomic_write(path: Path, text: str) -> Stat:
    """Write ``text`` to ``path`` atomically; return the stat of exactly these bytes.

    The stat is taken from the temporary file before it is renamed (a rename
    keeps size and mtime), so a concurrent edit after the rename cannot be
    recorded as DAKC's own write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dakc-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            st = os.fstat(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return st.st_size, st.st_mtime_ns


def _text_hash(path: Path) -> str | None:
    """Hash of the file's text, or None when it cannot be read as UTF-8 text (then it differs)."""
    try:
        return h(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _rendered(store: Store, artifact_id: str) -> str:
    row = store.conn.execute("SELECT payload FROM artifacts WHERE id=? AND to_build IS NULL", (artifact_id,)).fetchone()
    return json.loads(row[0])["rendered"]


def _desired(store: Store) -> dict[str, tuple[str, str]]:
    """rel path -> (artifact id, content hash) for every projected artifact at HEAD."""
    return {
        rel_path(aid, kind): (aid, chash)
        for aid, kind, chash in store.conn.execute(
            "SELECT id, kind, content_hash FROM artifacts WHERE to_build IS NULL AND kind IN ('page','summary','index')"
        )
    }


def sync_projection(store: Store, output_root: Path, state_dir: Path, *, verify_files: bool = True) -> dict[str, Any]:
    """Bring ``output_root`` in line with the store.

    Returns the file operations: ``written`` (the artifact changed, or the file
    is new to the manifest), ``repaired`` (the artifact did not change but the
    file was edited or deleted by hand, so it was restored), ``deleted`` (its
    artifact no longer exists), ``unchanged``, and ``repaired_paths`` (sorted,
    relative to ``output_root``). ``written + repaired + unchanged`` is the
    number of projected files.

    With ``verify_files=False``, an existing file whose artifact did not change
    is trusted without looking at its stat; a missing one is still restored.
    """
    manifest_path = state_dir / MANIFEST
    manifest: dict[str, str] = {}
    stats: dict[str, list[int]] = {}
    manifest_mtime: int | None = None
    if manifest_path.exists():
        manifest_mtime = manifest_path.stat().st_mtime_ns
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = data.get("files", {})
        stats = data.get("stat", {})  # absent in manifests written before 0.8: every file is hashed once

    desired = _desired(store)
    new_manifest: dict[str, str] = {}
    new_stats: dict[str, list[int]] = {}
    unchanged = 0
    to_write: list[tuple[str, str]] = []  # the artifact changed (or the file is new to the manifest)
    to_repair: list[tuple[str, str]] = []  # the artifact did not change, the file did
    for rel, (aid, chash) in desired.items():
        new_manifest[rel] = chash
        if manifest.get(rel) != chash:
            to_write.append((rel, aid))
            continue
        path = output_root / rel
        try:
            st = os.stat(path)  # before reading: an edit after this point changes the mtime we record
        except FileNotFoundError:
            to_repair.append((rel, aid))
            continue
        recorded = stats.get(rel)
        current = [st.st_size, st.st_mtime_ns]
        expected = manifest.get(f"{rel}#text")
        if not verify_files or (recorded == current and manifest_mtime is not None and st.st_mtime_ns < manifest_mtime):
            ok = True  # unchanged since DAKC last wrote or checked it
            current = recorded or current
        else:
            if expected is None:  # a manifest entry without a text hash: compare with the store
                expected = h(_rendered(store, aid))
            ok = _text_hash(path) == expected
        if ok:
            unchanged += 1
            if expected is not None:
                new_manifest[f"{rel}#text"] = expected
            if verify_files or recorded is not None:
                new_stats[rel] = current
        else:
            to_repair.append((rel, aid))

    for rel, aid in to_write + to_repair:
        text = _rendered(store, aid)
        size, mtime_ns = _atomic_write(output_root / rel, text)
        new_manifest[f"{rel}#text"] = h(text)
        new_stats[rel] = [size, mtime_ns]

    deleted = 0
    for rel in sorted(k for k in manifest if not k.endswith("#text")):
        if rel not in desired:
            p = output_root / rel
            if p.exists():
                p.unlink()
                deleted += 1

    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = manifest_path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"head": store.head, "files": new_manifest, "stat": new_stats}, sort_keys=True, indent=0),
                   encoding="utf-8")
    os.replace(tmp, manifest_path)
    return {
        "written": len(to_write),
        "repaired": len(to_repair),
        "deleted": deleted,
        "unchanged": unchanged,
        "repaired_paths": sorted(rel for rel, _ in to_repair),
    }


def drifted_files(store: Store, output_root: Path) -> list[tuple[str, str]]:
    """``(rel path, "missing" | "differs")`` for every projected file whose text is not
    the store's rendering. Reads and hashes every file (no stat shortcut): this is
    the audit ``dakc verify`` runs, and it changes nothing."""
    out = []
    for rel, (aid, _) in sorted(_desired(store).items()):
        path = output_root / rel
        if not path.exists():
            out.append((rel, "missing"))
        elif _text_hash(path) != h(_rendered(store, aid)):
            out.append((rel, "differs"))
    return out
