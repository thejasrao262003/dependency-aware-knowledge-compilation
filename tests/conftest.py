from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # make `benchmarks` importable
    sys.path.insert(0, str(ROOT))

from dakc.compiler import Compiler  # noqa: E402
from dakc.providers import make_provider  # noqa: E402
from dakc.storage import Store  # noqa: E402


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def compile_once(store: Store, src: Path, strategy: str = "dakc", provider: str = "mock", **kw):
    report, state = Compiler(store, make_provider(provider), src, strategy=strategy, **kw).run()
    return report


@pytest.fixture
def kb(tmp_path: Path):
    """A small source tree + store. Returns (src dir, store)."""
    src = tmp_path / "knowledge"
    src.mkdir()
    store = Store(tmp_path / ".dakc" / "dakc.db")
    yield src, store
    store.close()


POSTGRES_DOCS = {
    # The PRD §5 example: one paragraph holding three claims, each feeding a
    # different page, and an overview (PostgreSQL) depending on all three.
    "databases/postgres.md": (
        "# PostgreSQL\n\n"
        "PostgreSQL supports Transactions. PostgreSQL supports JSONB. PostgreSQL supports Indexing.\n"
    ),
    "databases/redis.md": "# Redis\n\nRedis stores data in memory. Redis supports Replication.\n",
    "messaging/kafka.md": "# Kafka\n\nKafka streams events to PostgreSQL. Kafka supports Replication.\n",
}
