"""Compilation transactions: atomic commit, rollback, digests, fault injection.

The compiler produces a :class:`ChangeSet` in memory. :func:`commit` applies it
inside one ``BEGIN IMMEDIATE`` transaction: superseded rows are closed
(``to_build = B``), new versions inserted, edges replaced, the build marked
``committed`` and ``HEAD`` advanced. An exception anywhere (including an
injected fault half-way through the writes) rolls the whole transaction back.

:func:`rollback` restores the exact state as of an earlier committed build by
deleting rows created after it and reopening rows closed after it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import blake3

from dakc.artifacts import Artifact
from dakc.errors import ConcurrentBuildError, RollbackError  # re-exported
from dakc.ingestion import SourceRecord
from dakc.storage import Store, now_iso


class InjectedFault(RuntimeError):
    """Raised by :class:`FaultInjector` to simulate a failure."""


class FaultInjector:
    """Raise :class:`InjectedFault` at the N-th event of a named point.

    Spec format: ``point:n[,point:n...]`` where point is one of ``extract``,
    ``page``, ``summary``, ``commit``. ``commit:n`` fires after ``n`` rows have
    been written inside the commit transaction. Read from ``DAKC_FAULT`` when
    not given explicitly, so the CLI can be exercised end to end.
    """

    def __init__(self, spec: str | None = None):
        spec = spec if spec is not None else os.environ.get("DAKC_FAULT", "")
        self.targets: dict[str, int] = {}
        self.hard_exit: set[str] = set()
        self.pause: set[str] = set()
        for part in filter(None, (p.strip() for p in spec.split(","))):
            point, n, mode = (part.split(":") + ["", ""])[:3]
            self.targets[point] = int(n or 1)
            if mode == "exit":  # simulate a killed process instead of an exception
                self.hard_exit.add(point)
            elif mode == "pause":  # hold the build open for 2 s (concurrency tests)
                self.pause.add(point)
        self.counts: dict[str, int] = {}

    def tick(self, point: str, n: int = 1) -> None:
        if point not in self.targets:
            return
        self.counts[point] = self.counts.get(point, 0) + n
        if self.counts[point] >= self.targets[point]:
            if point in self.pause:
                import time

                if self.counts[point] - n < self.targets[point]:
                    time.sleep(2.0)
                return
            if point in self.hard_exit:
                os._exit(137)  # no cleanup, no rollback: like SIGKILL / power loss
            raise InjectedFault(f"injected fault at {point} #{self.counts[point]}")


@dataclass
class ChangeSet:
    upserts: list[Artifact] = field(default_factory=list)
    deletes: list[tuple[str, str]] = field(default_factory=list)  # (id, kind)
    edges: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # dependent -> new deps ([] = none)
    source_upserts: list[SourceRecord] = field(default_factory=list)
    source_deletes: list[str] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)
    # conflicts (RFC 0002): advisory findings and the index they are found by
    conflict_upserts: list[tuple[str, str, str, str]] = field(default_factory=list)  # (id, key, kind, payload)
    conflict_deletes: list[str] = field(default_factory=list)
    conflict_index_reset: bool = False
    conflict_index_claims: list[str] = field(default_factory=list)  # claims whose index rows are replaced
    conflict_index_rows: list[tuple] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.upserts or self.deletes or self.edges or self.source_upserts or self.source_deletes
                    or self.conflict_upserts or self.conflict_deletes or self.conflict_index_reset
                    or self.conflict_index_claims or self.meta)

    def counts(self) -> dict[str, int]:
        by_kind: dict[str, int] = {}
        for a in self.upserts:
            by_kind[a.kind] = by_kind.get(a.kind, 0) + 1
        return {
            "artifact_versions_written": len(self.upserts),
            "artifacts_deleted": len(self.deletes),
            "edge_sets_replaced": len(self.edges),
            "source_rows_written": len(self.source_upserts) + len(self.source_deletes),
            **{f"written_{k}": v for k, v in sorted(by_kind.items())},
        }


def commit(
    store: Store,
    build_id: int,
    cs: ChangeSet,
    versions: dict[str, int],
    stats: dict,
    fault: FaultInjector | None = None,
    expected_head: int | None = None,
) -> None:
    fault = fault or FaultInjector("")
    with store.transaction() as c:
        if expected_head is not None and store.head != expected_head:
            raise ConcurrentBuildError(
                f"HEAD moved from build {expected_head} to {store.head} while this build was computing; nothing was committed"
            )
        written = 0

        def tick(n: int) -> None:
            nonlocal written
            written += n
            fault.tick("commit", n)

        # sources
        if cs.source_upserts or cs.source_deletes:
            ids = [r.id for r in cs.source_upserts] + cs.source_deletes
            c.executemany("UPDATE sources SET to_build=? WHERE id=? AND to_build IS NULL", [(build_id, i) for i in ids])
            c.executemany(
                "INSERT INTO sources(id,path,raw_hash,text_hash,size,type,key,revision,from_build) VALUES(?,?,?,?,?,?,?,?,?)",
                [(r.id, r.path, r.raw_hash, r.text_hash, r.size, r.type, r.key, r.revision, build_id) for r in cs.source_upserts],
            )
            tick(len(ids))

        # artifacts (in chunks so a commit fault can land mid-way)
        upserts = sorted(cs.upserts, key=lambda a: a.id)
        for i in range(0, len(upserts), 1000):
            chunk = upserts[i : i + 1000]
            c.executemany("UPDATE artifacts SET to_build=? WHERE id=? AND to_build IS NULL", [(build_id, a.id) for a in chunk])
            c.executemany(
                "INSERT INTO artifacts(id,kind,version,content_hash,state_hash,payload,from_build) VALUES(?,?,?,?,?,?,?)",
                [
                    (a.id, a.kind, versions.get(a.id, 0) + 1, a.content_hash, a.state_hash,
                     json.dumps(a.payload(), sort_keys=True, ensure_ascii=False), build_id)
                    for a in chunk
                ],
            )
            tick(len(chunk))
        if cs.deletes:
            c.executemany("UPDATE artifacts SET to_build=? WHERE id=? AND to_build IS NULL", [(build_id, i) for i, _ in cs.deletes])
            tick(len(cs.deletes))

        # edges
        dependents = sorted(cs.edges)
        for i in range(0, len(dependents), 1000):
            chunk = dependents[i : i + 1000]
            c.executemany("UPDATE edges SET to_build=? WHERE dependent=? AND to_build IS NULL", [(build_id, d) for d in chunk])
            rows = [(d, dep, kind, build_id) for d in chunk for dep, kind in cs.edges[d]]
            c.executemany("INSERT INTO edges(dependent,dependency,kind,from_build) VALUES(?,?,?,?)", rows)
            tick(len(chunk))

        # conflicts: versioned findings, and the derived index (replaced, not versioned)
        if cs.conflict_index_reset:
            c.execute("DELETE FROM conflict_index")
        for i in range(0, len(cs.conflict_index_claims), 500):
            chunk = cs.conflict_index_claims[i:i + 500]
            c.execute(f"DELETE FROM conflict_index WHERE claim_id IN ({','.join('?' * len(chunk))})", chunk)
        if cs.conflict_index_rows:
            c.executemany("INSERT INTO conflict_index(key, claim_id, passage_id, value, polarity, scope, path, heading, sentence) "
                          "VALUES(?,?,?,?,?,?,?,?,?)", cs.conflict_index_rows)
        closed = [fid for fid, *_ in cs.conflict_upserts] + cs.conflict_deletes
        c.executemany("UPDATE conflicts SET to_build=? WHERE id=? AND to_build IS NULL", [(build_id, i) for i in closed])
        c.executemany("INSERT INTO conflicts(id, key, kind, payload, from_build) VALUES(?,?,?,?,?)",
                      [(fid, key, kind, payload, build_id) for fid, key, kind, payload in cs.conflict_upserts])

        for k, v in cs.meta.items():
            store.set_meta(k, v)
        store.set_meta("head", str(build_id))
        c.execute(
            "UPDATE builds SET status='committed', finished_at=?, stats=? WHERE id=?",
            (now_iso(), json.dumps(stats, sort_keys=True), build_id),
        )


def rollback(store: Store, to_build: int) -> int:
    """Restore the state as of committed build ``to_build``. Returns the new build id."""
    target = store.build(to_build)
    if to_build != 0 and (target is None or target.status != "committed"):
        raise RollbackError(f"build {to_build} is not a committed build")
    if to_build == store.head:
        raise RollbackError(f"build {to_build} is already HEAD")
    with store.transaction() as c:
        for table in ("artifacts", "edges", "sources", "conflicts"):
            c.execute(f"DELETE FROM {table} WHERE from_build > ?", (to_build,))
            c.execute(f"UPDATE {table} SET to_build=NULL WHERE to_build > ?", (to_build,))
        c.execute("DELETE FROM conflict_index")  # derived: rebuilt by the next build
        store.set_meta("conflicts_fp", "")
        c.execute("UPDATE builds SET status='rolled_back' WHERE id > ? AND status='committed'", (to_build,))
        cur = c.execute(
            "INSERT INTO builds(parent,strategy,mode,status,started_at,finished_at,stats) VALUES(?,?,?,?,?,?,?)",
            (to_build or None, "rollback", "rollback", "committed", now_iso(), now_iso(),
             json.dumps({"restored_to": to_build, "config_fingerprint": (target.stats.get("config_fingerprint", "") if target else "")})),
        )
        new_id = int(cur.lastrowid)
        store.set_meta("head", str(new_id))
        store.set_meta("config_fingerprint", target.stats.get("config_fingerprint", "") if target else "")
        store.set_meta("compiler_versions", json.dumps(target.stats.get("compiler_versions", {}) if target else {}, sort_keys=True))
    return new_id


def state_digest(store: Store) -> str:
    """A hash of the complete current knowledge state (sources, artifacts, edges)."""
    hasher = blake3.blake3()
    for row in store.conn.execute(
        "SELECT id,path,raw_hash,text_hash FROM sources WHERE to_build IS NULL ORDER BY id"
    ):
        hasher.update(("S\x1f" + "\x1f".join(row) + "\n").encode())
    for row in store.conn.execute("SELECT id,state_hash FROM artifacts WHERE to_build IS NULL ORDER BY id"):
        hasher.update(("A\x1f" + "\x1f".join(row) + "\n").encode())
    for row in store.conn.execute(
        "SELECT dependent,dependency,kind FROM edges WHERE to_build IS NULL ORDER BY dependent,dependency,kind"
    ):
        hasher.update(("E\x1f" + "\x1f".join(row) + "\n").encode())
    return hasher.hexdigest()
