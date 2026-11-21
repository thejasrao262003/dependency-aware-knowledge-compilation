"""Hashing, canonical serialization and text normalization.

Everything that feeds an id or a signature goes through this module so that
identities are stable across platforms, runs and Python versions.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

import blake3

ID_HEX = 16  # 64-bit id prefixes (see ADR-8)

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "})
_WS = re.compile(r"\s+")
_TRAILING = re.compile(r"[\s.;:!?,]+$")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def h(data: bytes | str) -> str:
    """Full BLAKE3 hex digest of bytes or UTF-8 text."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return blake3.blake3(data).hexdigest()


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, tuples as lists."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default)


def _default(o: Any) -> Any:
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(f"not serializable: {type(o).__name__}")


def h_obj(obj: Any) -> str:
    """Hash of the canonical JSON form of ``obj``."""
    return h(canonical_json(obj))


def short(data: bytes | str) -> str:
    return h(data)[:ID_HEX]


def hash_file(path: Path) -> str:
    hasher = blake3.blake3()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def normalize_text(text: str) -> str:
    """NFKC, unify quotes/dashes, collapse whitespace. Case is preserved."""
    text = unicodedata.normalize("NFKC", text).translate(_QUOTES)
    return _WS.sub(" ", text).strip()


def normalize_claim(text: str) -> str:
    """Identity form of a claim: normalized, case-folded, no trailing punctuation."""
    return _TRAILING.sub("", normalize_text(text).casefold())


def claim_id(text: str) -> str:
    return "clm:" + short(normalize_claim(text))


def entity_key(name: str) -> str:
    """Case- and punctuation-insensitive key for an entity name."""
    key = _NON_ALNUM.sub("", unicodedata.normalize("NFKC", name).casefold())
    return key or "x" + short(name)[:8]


def slugify(text: str) -> str:
    slug = _NON_ALNUM.sub("-", unicodedata.normalize("NFKC", text).casefold()).strip("-")
    return slug or "item"
