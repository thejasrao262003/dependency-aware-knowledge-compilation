"""SQLite store with temporal tables.

``sources``, ``artifacts`` and ``edges`` rows carry ``from_build`` and
``to_build``. The current state is ``to_build IS NULL``; the state as of build
``N`` is ``from_build <= N AND (to_build IS NULL OR to_build > N)``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dakc.artifacts import Artifact, from_payload
from dakc.ingestion import SourceRecord

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS builds(
    id INTEGER PRIMARY KEY,
    parent INTEGER,
    strategy TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS sources(
    id TEXT NOT NULL, path TEXT NOT NULL, raw_hash TEXT NOT NULL, text_hash TEXT NOT NULL,
    size INTEGER NOT NULL, type TEXT NOT NULL,
    from_build INTEGER NOT NULL, to_build INTEGER
);
CREATE INDEX IF NOT EXISTS sources_cur ON sources(id) WHERE to_build IS NULL;
CREATE INDEX IF NOT EXISTS sources_from ON sources(from_build);
CREATE INDEX IF NOT EXISTS sources_to ON sources(to_build);
CREATE TABLE IF NOT EXISTS artifacts(
    id TEXT NOT NULL, kind TEXT NOT NULL, version INTEGER NOT NULL,
    content_hash TEXT NOT NULL, state_hash TEXT NOT NULL, payload TEXT NOT NULL,
    from_build INTEGER NOT NULL, to_build INTEGER
);
CREATE INDEX IF NOT EXISTS artifacts_cur ON artifacts(id) WHERE to_build IS NULL;
CREATE INDEX IF NOT EXISTS artifacts_kind ON artifacts(kind) WHERE to_build IS NULL;
CREATE INDEX IF NOT EXISTS artifacts_hist ON artifacts(id, version);
CREATE INDEX IF NOT EXISTS artifacts_from ON artifacts(from_build);
CREATE INDEX IF NOT EXISTS artifacts_to ON artifacts(to_build);
CREATE TABLE IF NOT EXISTS edges(
    dependent TEXT NOT NULL, dependency TEXT NOT NULL, kind TEXT NOT NULL,
    from_build INTEGER NOT NULL, to_build INTEGER
);
CREATE INDEX IF NOT EXISTS edges_dependency ON edges(dependency) WHERE to_build IS NULL;
CREATE INDEX IF NOT EXISTS edges_dependent ON edges(dependent) WHERE to_build IS NULL;
CREATE INDEX IF NOT EXISTS edges_from ON edges(from_build);
CREATE INDEX IF NOT EXISTS edges_to ON edges(to_build);
CREATE TABLE IF NOT EXISTS extraction_cache(
    key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class BuildRecord:
    id: int
    parent: int | None
    strategy: str
    mode: str
    status: str
    started_at: str
    finished_at: str | None
    stats: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class KBState:
    """In-memory snapshot of the current (HEAD) state."""

    sources: dict[str, SourceRecord] = field(default_factory=dict)
    artifacts: dict[str, dict[str, Artifact]] = field(default_factory=dict)  # kind -> id -> artifact
    versions: dict[str, int] = field(default_factory=dict)  # artifact id -> current version
    config_fingerprint: str = ""
    compiler_versions: dict[str, str] = field(default_factory=dict)  # dakc.versions recorded at HEAD

    def of(self, kind: str) -> dict[str, Artifact]:
        return self.artifacts.setdefault(kind, {})


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.executescript(SCHEMA)
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    @property
    def head(self) -> int:
        v = self.get_meta("head")
        return int(v) if v else 0

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ---------------------------------------------------------------- builds
    def begin_build(self, strategy: str, mode: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO builds(parent,strategy,mode,status,started_at) VALUES(?,?,?,?,?)",
            (self.head or None, strategy, mode, "running", now_iso()),
        )
        return int(cur.lastrowid)

    def finish_build(self, build_id: int, status: str, stats: dict | None = None, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE builds SET status=?, finished_at=?, stats=?, error=? WHERE id=?",
            (status, now_iso(), json.dumps(stats or {}, sort_keys=True), error, build_id),
        )

    def recover_aborted_builds(self) -> list[int]:
        """Mark builds left 'running' by a crashed process as 'aborted'."""
        ids = [r[0] for r in self.conn.execute("SELECT id FROM builds WHERE status='running'")]
        for bid in ids:
            self.finish_build(bid, "aborted", error="process exited before the build finished")
        return ids

    def builds(self, limit: int | None = None) -> list[BuildRecord]:
        sql = "SELECT id,parent,strategy,mode,status,started_at,finished_at,stats,error FROM builds ORDER BY id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [
            BuildRecord(r[0], r[1], r[2], r[3], r[4], r[5], r[6], json.loads(r[7]) if r[7] else {}, r[8])
            for r in self.conn.execute(sql)
        ]

    def build(self, build_id: int) -> BuildRecord | None:
        for b in self.builds():
            if b.id == build_id:
                return b
        return None

    # ------------------------------------------------------------ load state
    def load_state(self) -> KBState:
        state = KBState(
            config_fingerprint=self.get_meta("config_fingerprint") or "",
            compiler_versions=json.loads(self.get_meta("compiler_versions") or "{}"),
        )
        for r in self.conn.execute("SELECT id,path,raw_hash,text_hash,size,type FROM sources WHERE to_build IS NULL"):
            state.sources[r[0]] = SourceRecord(*r)
        for id_, kind, version, payload in self.conn.execute(
            "SELECT id,kind,version,payload FROM artifacts WHERE to_build IS NULL"
        ):
            state.of(kind)[id_] = from_payload(kind, id_, json.loads(payload))
            state.versions[id_] = version
        return state

    def load_sources(self) -> dict[str, SourceRecord]:
        return {
            r[0]: SourceRecord(*r)
            for r in self.conn.execute("SELECT id,path,raw_hash,text_hash,size,type FROM sources WHERE to_build IS NULL")
        }

    def all_source_ids(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT DISTINCT id FROM sources")}

    # ----------------------------------------------------------------- cache
    def cache_get_many(self, keys: Iterable[str]) -> dict[str, Any]:
        keys = list(keys)
        out: dict[str, Any] = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i : i + 500]
            q = f"SELECT key,result FROM extraction_cache WHERE key IN ({','.join('?' * len(chunk))})"
            for k, v in self.conn.execute(q, chunk):
                out[k] = json.loads(v)
        return out

    def cache_put_many(self, items: list[tuple[str, Any]], fingerprint: str) -> None:
        ts = now_iso()
        with self.transaction() as c:
            c.executemany(
                "INSERT OR IGNORE INTO extraction_cache(key,fingerprint,result,created_at) VALUES(?,?,?,?)",
                [(k, fingerprint, json.dumps(v, sort_keys=True), ts) for k, v in items],
            )

    def cache_size(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM extraction_cache").fetchone()[0]

    # --------------------------------------------------------------- queries
    def current_artifact(self, artifact_id: str) -> tuple[Artifact, int, int] | None:
        row = self.conn.execute(
            "SELECT kind,payload,version,from_build FROM artifacts WHERE id=? AND to_build IS NULL", (artifact_id,)
        ).fetchone()
        if not row:
            return None
        return from_payload(row[0], artifact_id, json.loads(row[1])), row[2], row[3]

    def artifact_history(self, artifact_id: str) -> list[dict[str, Any]]:
        return [
            {"version": r[0], "content_hash": r[1], "from_build": r[2], "to_build": r[3]}
            for r in self.conn.execute(
                "SELECT version,content_hash,from_build,to_build FROM artifacts WHERE id=? ORDER BY version", (artifact_id,)
            )
        ]

    def current_source(self, source_id: str) -> SourceRecord | None:
        r = self.conn.execute(
            "SELECT id,path,raw_hash,text_hash,size,type FROM sources WHERE id=? AND to_build IS NULL", (source_id,)
        ).fetchone()
        return SourceRecord(*r) if r else None

    def dependencies(self, artifact_id: str) -> list[tuple[str, str]]:
        return list(self.conn.execute(
            "SELECT dependency,kind FROM edges WHERE dependent=? AND to_build IS NULL ORDER BY dependency", (artifact_id,)
        ))

    def dependents(self, artifact_id: str) -> list[tuple[str, str]]:
        return list(self.conn.execute(
            "SELECT dependent,kind FROM edges WHERE dependency=? AND to_build IS NULL ORDER BY dependent", (artifact_id,)
        ))

    def dependents_many(self, ids: Iterable[str]) -> list[tuple[str, str, str]]:
        ids = list(ids)
        out: list[tuple[str, str, str]] = []
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            q = f"SELECT dependent,dependency,kind FROM edges WHERE to_build IS NULL AND dependency IN ({','.join('?' * len(chunk))})"
            out.extend(self.conn.execute(q, chunk))
        return out

    def dependencies_many(self, ids: Iterable[str]) -> list[tuple[str, str, str]]:
        ids = list(ids)
        out: list[tuple[str, str, str]] = []
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            q = f"SELECT dependent,dependency,kind FROM edges WHERE to_build IS NULL AND dependent IN ({','.join('?' * len(chunk))})"
            out.extend(self.conn.execute(q, chunk))
        return out

    def current_edges(self) -> list[tuple[str, str, str]]:
        return list(self.conn.execute("SELECT dependent,dependency,kind FROM edges WHERE to_build IS NULL"))

    def counts(self) -> dict[str, int]:
        out = {"source": self.conn.execute("SELECT COUNT(*) FROM sources WHERE to_build IS NULL").fetchone()[0]}
        for kind, n in self.conn.execute("SELECT kind, COUNT(*) FROM artifacts WHERE to_build IS NULL GROUP BY kind"):
            out[kind] = n
        out["edges"] = self.conn.execute("SELECT COUNT(*) FROM edges WHERE to_build IS NULL").fetchone()[0]
        return out

    def changes_in_build(self, build_id: int) -> dict[str, list[tuple[str, str]]]:
        """Artifacts created, updated or deleted by one build: kind -> [(id, change)]."""
        out: dict[str, list[tuple[str, str]]] = {}
        new_rows = {
            r[0]: (r[1], r[2])
            for r in self.conn.execute("SELECT id,kind,version FROM artifacts WHERE from_build=?", (build_id,))
        }
        closed = {r[0]: r[1] for r in self.conn.execute("SELECT id,kind FROM artifacts WHERE to_build=?", (build_id,))}
        for id_, (kind, version) in new_rows.items():
            out.setdefault(kind, []).append((id_, "created" if version == 1 else "updated"))
        for id_, kind in closed.items():
            if id_ not in new_rows:
                out.setdefault(kind, []).append((id_, "deleted"))
        for v in out.values():
            v.sort()
        return out

    def diff_builds(self, a: int, b: int) -> dict[str, list[tuple[str, str]]]:
        """Artifacts that differ between the states as of build ``a`` and ``b``."""
        def state_at(n: int) -> dict[str, tuple[str, str]]:
            return {
                r[0]: (r[1], r[2])
                for r in self.conn.execute(
                    "SELECT id,kind,state_hash FROM artifacts WHERE from_build<=? AND (to_build IS NULL OR to_build>?)", (n, n)
                )
            }
        sa, sb = state_at(a), state_at(b)
        out: dict[str, list[tuple[str, str]]] = {}
        for id_ in sorted(set(sa) | set(sb)):
            if id_ not in sa:
                out.setdefault(sb[id_][0], []).append((id_, "created"))
            elif id_ not in sb:
                out.setdefault(sa[id_][0], []).append((id_, "deleted"))
            elif sa[id_][1] != sb[id_][1]:
                out.setdefault(sb[id_][0], []).append((id_, "updated"))
        return out
