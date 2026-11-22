"""Publishers: where compiled knowledge goes (RFC 0001, ``docs/rfcs/0001-sources-and-publishers.md``).

A publisher delivers artifacts to a target (a change feed, a vector database,
a graph database). DAKC keeps a **ledger** per publisher: the ``state_hash`` of
every artifact the publisher has acknowledged. What to send is always the
difference between the ledger and the current state:

* current artifacts of the kinds it wants whose state differs   -> upserts
* ledger entries that are no longer current (or no longer wanted) -> deletes

Batches are recorded in the ledger only after ``publish`` returns, so a
failure leaves the rest pending and the next run resumes. Rollbacks, full
rebuilds and newly added publishers need no special case. Publishers must be
idempotent: upserts are keyed by artifact id and deletes may repeat.

The wiki folder is not a publisher in this sense: it is the built-in Markdown
projection (:mod:`dakc.compiler.projection`), which reconciles with its own
manifest and is always on.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from dakc.artifacts import LAYER
from dakc.errors import PublishError
from dakc.sources import PUBLISHER_GROUP, load_factory
from dakc.storage import Store, now_iso

PUBLISHABLE = ("passage", "claim", "page", "summary", "index")
DEFAULT_KINDS = frozenset({"claim", "page", "summary"})


@dataclass(frozen=True)
class Record:
    """One artifact as a publisher receives it."""

    id: str  # DAKC artifact id (claims are content-addressed)
    kind: str  # passage | claim | page | summary | index
    state_hash: str  # changes whenever anything a consumer can see changes
    title: str
    text: str  # rendered Markdown for pages, summaries and the index; the text otherwise
    sources: list[str]  # source document paths it derives from (passages, claims, pages)
    build: int  # the build that wrote this version
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PublishBatch:
    upserts: list[Record]
    deletes: list[str]  # artifact ids no longer current


@runtime_checkable
class Publisher(Protocol):
    """What a publisher plugin implements. ``publish`` must be idempotent and raise on failure."""

    kind: str
    kinds: frozenset[str]

    def publish(self, batch: PublishBatch) -> None: ...


@dataclass
class PublishResult:
    name: str
    kind: str
    upserted: int = 0
    deleted: int = 0
    pending: int = 0  # still to do after this run (non-zero only on error or dry run)
    error: str | None = None
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ok": self.ok}


# ------------------------------------------------------------------ ledger
def pending(store: Store, name: str, kinds: Iterable[str]) -> tuple[list[tuple[str, str, str, int]], list[str]]:
    """(upserts as (id, kind, state_hash, build), deletes) for publisher ``name``."""
    kinds = sorted(set(kinds) & set(PUBLISHABLE))
    marks = ",".join("?" * len(kinds))
    current = {
        r[0]: r for r in store.conn.execute(
            f"SELECT id, kind, state_hash, from_build FROM artifacts WHERE to_build IS NULL AND kind IN ({marks})", kinds)
    } if kinds else {}
    ledger = dict(store.conn.execute("SELECT id, state_hash FROM published WHERE publisher=?", (name,)))
    ups = sorted((r for aid, r in current.items() if ledger.get(aid) != r[2]), key=lambda r: (LAYER.get(r[1], 9), r[0]))
    dels = sorted(aid for aid in ledger if aid not in current)
    return ups, dels


def forget(store: Store, name: str) -> int:
    """Drop a publisher's ledger, so its next run republishes everything."""
    with store.transaction() as c:
        return c.execute("DELETE FROM published WHERE publisher=?", (name,)).rowcount


class _Resolver:
    """Loads what records need (passage -> source path, claim -> passages) once per run."""

    def __init__(self, store: Store):
        self.store = store
        self._psg: dict[str, str] | None = None
        self._src: dict[str, str] | None = None

    def _load(self) -> None:
        if self._psg is None:
            self._psg = dict(self.store.conn.execute(
                "SELECT id, json_extract(payload, '$.source_id') FROM artifacts WHERE kind='passage' AND to_build IS NULL"))
            self._src = dict(self.store.conn.execute("SELECT id, path FROM sources WHERE to_build IS NULL"))

    def source_path(self, sid: str) -> str | None:
        self._load()
        return self._src.get(sid)  # type: ignore[union-attr]

    def passage_source(self, pid: str) -> str | None:
        self._load()
        sid = self._psg.get(pid)  # type: ignore[union-attr]
        return self.source_path(sid) if sid else None

    def claim_supports(self, cids: list[str]) -> dict[str, list]:
        out: dict[str, list] = {}
        for i in range(0, len(cids), 500):
            chunk = cids[i:i + 500]
            q = f"SELECT id, payload FROM artifacts WHERE to_build IS NULL AND id IN ({','.join('?' * len(chunk))})"
            for cid, payload in self.store.conn.execute(q, chunk):
                out[cid] = json.loads(payload)["supports"]
        return out

    def paths(self, pids: Iterable[str]) -> list[str]:
        return sorted({p for p in (self.passage_source(x) for x in pids) if p})


def build_records(store: Store, rows: list[tuple[str, str, str, int]], resolver: _Resolver | None = None) -> list[Record]:
    res = resolver or _Resolver(store)
    payloads: dict[str, dict] = {}
    ids = [r[0] for r in rows]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = f"SELECT id, payload FROM artifacts WHERE to_build IS NULL AND id IN ({','.join('?' * len(chunk))})"
        payloads.update((aid, json.loads(p)) for aid, p in store.conn.execute(q, chunk))
    out: list[Record] = []
    for aid, kind, state_hash, build in rows:
        p = payloads[aid]
        if kind == "passage":
            src = res.source_path(p["source_id"])
            out.append(Record(aid, kind, state_hash, p.get("heading") or aid, p["text"], [src] if src else [], build,
                              {"heading": p.get("heading", ""), "source_id": p["source_id"]}))
        elif kind == "claim":
            sup = p["supports"]
            text = min(s[1] for s in sup) if sup else ""
            out.append(Record(aid, kind, state_hash, text, text, res.paths(s[0] for s in sup), build,
                              {"mentions": sorted({m for s in sup for m in s[2]}), "passages": [s[0] for s in sup]}))
        elif kind == "page":
            sup = res.claim_supports(p["claim_ids"])
            paths = res.paths(s[0] for c in sup.values() for s in c)
            out.append(Record(aid, kind, state_hash, p["title"], p["rendered"], paths, build,
                              {"entity_id": p["entity_id"], "topic": p.get("topic", ""), "lead": p.get("lead", ""),
                               "claim_ids": p["claim_ids"]}))
        elif kind == "summary":
            out.append(Record(aid, kind, state_hash, p["title"], p["rendered"], [], build,
                              {"topic": p.get("topic", ""), "page_ids": p.get("page_ids", [])}))
        else:  # index
            out.append(Record(aid, kind, state_hash, "Index", p["rendered"], [], build,
                              {"page_ids": p.get("page_ids", []), "summary_ids": p.get("summary_ids", [])}))
    return out


def publish(store: Store, name: str, publisher: Publisher, *, batch_size: int = 256, dry_run: bool = False) -> PublishResult:
    """Bring one publisher's target up to date with HEAD. Never raises for a failing
    publisher: its error is returned and the unsent work stays pending."""
    kinds = getattr(publisher, "kinds", None) or DEFAULT_KINDS
    ups, dels = pending(store, name, kinds)
    result = PublishResult(name, publisher.kind, pending=len(ups) + len(dels), dry_run=dry_run)
    if dry_run:
        return result
    resolver = _Resolver(store)
    try:
        for i in range(0, len(dels), batch_size):
            chunk = dels[i:i + batch_size]
            publisher.publish(PublishBatch([], chunk))
            with store.transaction() as c:
                c.executemany("DELETE FROM published WHERE publisher=? AND id=?", [(name, d) for d in chunk])
            result.deleted += len(chunk)
        for i in range(0, len(ups), batch_size):
            chunk = ups[i:i + batch_size]
            publisher.publish(PublishBatch(build_records(store, chunk, resolver), []))
            stamp = now_iso()
            with store.transaction() as c:
                c.executemany(
                    "INSERT OR REPLACE INTO published(publisher, id, kind, state_hash, build, published_at) VALUES(?,?,?,?,?,?)",
                    [(name, aid, kind, sh, b, stamp) for aid, kind, sh, b in chunk])
            result.upserted += len(chunk)
    except Exception as exc:  # a target being down must not fail the build that already committed
        result.error = f"{type(exc).__name__}: {exc}"
    result.pending = len(ups) + len(dels) - result.upserted - result.deleted
    return result


# --------------------------------------------------------------- built-ins
class JsonlPublisher:
    """A change feed: one JSON line per upsert (``{"op": "upsert", ...record}``) or
    delete (``{"op": "delete", "id": ...}``), appended to a file. The integration
    point for anything without a plugin: read new lines, update your target."""

    kind = "jsonl"

    def __init__(self, path: Path, kinds: Iterable[str] = DEFAULT_KINDS):
        self.path = path
        self.kinds = frozenset(kinds)

    def publish(self, batch: PublishBatch) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            for d in batch.deletes:
                fh.write(json.dumps({"op": "delete", "id": d}, ensure_ascii=False) + "\n")
            for r in batch.upserts:
                fh.write(json.dumps({"op": "upsert", **r.to_dict()}, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def _make_jsonl(name: str, options: dict[str, Any], root: Path) -> JsonlPublisher:
    if "path" not in options:
        raise PublishError(f'jsonl publisher {name!r} needs path = "..."')
    return JsonlPublisher((root / options["path"]).resolve(), options.get("kinds", DEFAULT_KINDS))


PUBLISHER_BUILTINS: dict[str, Callable] = {"jsonl": _make_jsonl}


def make_publisher(spec: dict[str, Any], root: Path) -> tuple[str, Publisher]:
    """Build a publisher from one ``[[publishers]]`` table."""
    name = spec.get("name")
    if not name or not isinstance(name, str):
        raise PublishError("every [[publishers]] entry needs a name")
    type_ = spec.get("type")
    if not type_:
        raise PublishError(f"publisher {name!r} needs a type (jsonl, custom, or an installed plugin)")
    options = {k: v for k, v in spec.items() if k not in {"name", "type"}}
    factory = load_factory(PUBLISHER_GROUP, type_, options, root, PUBLISHER_BUILTINS)
    kwargs = {k: v for k, v in options.items() if k not in {"factory", "plugin_path"}} if type_ == "custom" else options
    try:
        pub = factory(name, kwargs, root)
    except PublishError:
        raise
    except Exception as exc:
        raise PublishError(f"publisher {name!r} ({type_}) could not be created: {type(exc).__name__}: {exc}") from exc
    if not isinstance(pub, Publisher):
        raise PublishError(f"publisher {name!r}: {type(pub).__name__} lacks kind, kinds or publish()")
    bad = set(pub.kinds) - set(PUBLISHABLE)
    if bad:
        raise PublishError(f"publisher {name!r}: unknown kinds {sorted(bad)}; choose from {', '.join(PUBLISHABLE)}")
    return name, pub
