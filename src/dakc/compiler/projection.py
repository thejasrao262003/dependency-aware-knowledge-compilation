"""Project pages, summaries and the index from the store to Markdown files.

The projection is a pure function of the store's current state, so it can be
re-run at any time (after a crash, a rollback, or manual tampering). Only
files listed in the projection manifest are ever deleted, so user files in the
output directory are left alone.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from dakc.hashing import h
from dakc.storage import Store

MANIFEST = "projection.json"


def rel_path(artifact_id: str, kind: str) -> str:
    key = artifact_id.split(":", 1)[1]
    if kind == "page":
        return f"pages/{key}.md"
    if kind == "summary":
        return f"summaries/{key}.md"
    return "index.md"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dakc-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def sync_projection(store: Store, output_root: Path, state_dir: Path, *, verify_files: bool = False) -> dict[str, int]:
    """Bring ``output_root`` in line with the store. Returns counts of file operations."""
    manifest_path = state_dir / MANIFEST
    manifest: dict[str, str] = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")).get("files", {})

    desired: dict[str, tuple[str, str]] = {}  # rel path -> (artifact id, content hash of rendered text)
    for aid, kind, payload_hash in store.conn.execute(
        "SELECT id, kind, content_hash FROM artifacts WHERE to_build IS NULL AND kind IN ('page','summary','index')"
    ):
        desired[rel_path(aid, kind)] = (aid, payload_hash)

    written = deleted = unchanged = 0
    to_fetch: list[tuple[str, str]] = []
    for rel, (aid, chash) in desired.items():
        path = output_root / rel
        ok = manifest.get(rel) == chash and path.exists()
        if ok and verify_files:
            ok = h(path.read_text(encoding="utf-8")) == manifest.get(f"{rel}#text")
        if ok:
            unchanged += 1
        else:
            to_fetch.append((rel, aid))

    new_manifest = {rel: desired[rel][1] for rel in desired}
    for rel, aid in to_fetch:
        row = store.conn.execute("SELECT payload FROM artifacts WHERE id=? AND to_build IS NULL", (aid,)).fetchone()
        text = json.loads(row[0])["rendered"]
        _atomic_write(output_root / rel, text)
        new_manifest[f"{rel}#text"] = h(text)
        written += 1
    for rel in desired:
        key = f"{rel}#text"
        if key not in new_manifest and key in manifest:
            new_manifest[key] = manifest[key]

    for rel in sorted(k for k in manifest if not k.endswith("#text")):
        if rel not in desired:
            p = output_root / rel
            if p.exists():
                p.unlink()
                deleted += 1

    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = manifest_path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"head": store.head, "files": new_manifest}, sort_keys=True, indent=0), encoding="utf-8")
    os.replace(tmp, manifest_path)
    return {"written": written, "deleted": deleted, "unchanged": unchanged}
