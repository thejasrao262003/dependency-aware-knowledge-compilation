"""Canonical comparison of two knowledge-base states.

Source ids are stable across renames, so two stores that reached the same
corpus by different histories may use different ids for the same file. The
canonical form replaces source ids with current paths and passage ids with
``path#text-hash#occurrence``. All other artifact ids are content- or
name-derived and are compared as is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from dakc.storage import Store


def canonical_state(store: Store) -> dict[str, str]:
    """Map canonical key -> hash for every source, artifact and edge at HEAD."""
    out: dict[str, str] = {}
    src_path: dict[str, str] = {}
    for sid, path, raw_hash, text_hash in store.conn.execute(
        "SELECT id, path, raw_hash, text_hash FROM sources WHERE to_build IS NULL"
    ):
        src_path[sid] = path
        out[f"source:{path}"] = f"{raw_hash}:{text_hash}"

    psg: dict[str, str] = {}
    rows = list(store.conn.execute("SELECT id, kind, content_hash, state_hash, payload FROM artifacts WHERE to_build IS NULL"))
    for aid, kind, _ch, _sh, payload in rows:
        if kind == "passage":
            p = json.loads(payload)
            psg[aid] = f"{src_path.get(p['source_id'], p['source_id'])}#{p['text_hash'][:16]}#{p.get('occurrence', 0)}"

    def canon(i: str) -> str:
        if i.startswith("src:"):
            return "src@" + src_path.get(i, i)
        if i.startswith("psg:"):
            return "psg@" + psg.get(i, i)
        return i

    for aid, kind, content_hash, state_hash, payload in rows:
        if kind == "passage":
            p = json.loads(payload)
            out[f"passage:{psg[aid]}"] = h_text(p["text"]) + ":" + h_text(p.get("heading", ""))
        elif kind == "claim":
            p = json.loads(payload)
            supports = sorted([canon(s[0]), s[1], s[2], s[3] if len(s) > 3 else []] for s in p["supports"])
            out[f"claim:{aid}"] = content_hash + ":" + json.dumps(supports, sort_keys=True)
        else:
            out[f"{kind}:{aid}"] = state_hash
    for dependent, dependency, kind in store.current_edges():
        out[f"edge:{canon(dependent)}->{canon(dependency)}:{kind}"] = "1"
    return out


def h_text(text: str) -> str:
    from dakc.hashing import h

    return h(text)


@dataclass
class StateDiff:
    missing: list[str] = field(default_factory=list)  # in reference, not in candidate
    extra: list[str] = field(default_factory=list)  # in candidate, not in reference
    different: list[str] = field(default_factory=list)

    @property
    def equal(self) -> bool:
        return not (self.missing or self.extra or self.different)

    def by_kind(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for label, keys in (("missing", self.missing), ("extra", self.extra), ("different", self.different)):
            for k in keys:
                kind = k.split(":", 1)[0]
                out.setdefault(kind, {}).setdefault(label, 0)
                out[kind][label] += 1
        return out

    def artifact_keys(self) -> set[str]:
        return {k for k in self.missing + self.extra + self.different if not k.startswith("edge:")}


def diff_states(reference: dict[str, str], candidate: dict[str, str]) -> StateDiff:
    d = StateDiff()
    for k in sorted(reference.keys() | candidate.keys()):
        if k not in candidate:
            d.missing.append(k)
        elif k not in reference:
            d.extra.append(k)
        elif reference[k] != candidate[k]:
            d.different.append(k)
    return d


def canonical_ids(store: Store) -> dict[str, str]:
    """Current artifact id -> canonical key (as used by :func:`canonical_state`)."""
    src_path = {sid: path for sid, path in store.conn.execute("SELECT id, path FROM sources WHERE to_build IS NULL")}
    out: dict[str, str] = {}
    for aid, kind, payload in store.conn.execute("SELECT id, kind, payload FROM artifacts WHERE to_build IS NULL"):
        if kind == "passage":
            p = json.loads(payload)
            out[aid] = f"passage:{src_path.get(p['source_id'], p['source_id'])}#{p['text_hash'][:16]}#{p.get('occurrence', 0)}"
        else:
            out[aid] = f"{kind}:{aid}"
    return out
