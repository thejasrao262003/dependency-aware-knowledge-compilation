"""Concurrent builds: the build lock serializes them; a stale build never commits."""

import subprocess
import sys
import time

import pytest

from dakc.compiler import Compiler
from dakc.providers import make_provider
from dakc.provenance.compare import canonical_state, diff_states
from dakc.storage import Store
from dakc.transactions import ConcurrentBuildError, state_digest
from dakc.transactions.lock import BuildLock, BuildLocked
from dakc.workspace import Workspace

from tests.conftest import POSTGRES_DOCS, compile_once, write_files


def test_stale_build_refuses_to_commit_after_concurrent_commit(kb):
    """Interleaving: B computes from HEAD=1, A commits HEAD=2 meanwhile, B must not commit."""
    src, store = kb
    write_files(src, POSTGRES_DOCS)
    compile_once(store, src)

    class Interleaved(Compiler):
        def _compute(self, old, sr, report):
            result = super()._compute(old, sr, report)
            write_files(src, {"messaging/nats.md": "NATS streams events to Kafka.\n"})
            Compiler(store, make_provider("mock"), src, acquire_lock=False).run()  # the "other" writer
            return result

    write_files(src, {"databases/redis.md": "Redis supports Clustering.\n"})
    after_other = None
    with pytest.raises(ConcurrentBuildError):
        Interleaved(store, make_provider("mock"), src, acquire_lock=False).run()
    after_other = state_digest(store)
    builds = store.builds()  # newest first: [other writer (committed), stale build (failed), initial]
    assert [b.status for b in builds[:2]] == ["committed", "failed"]
    assert "HEAD moved" in builds[1].error
    # The stale build changed nothing; the store holds exactly what the other writer committed
    # (which already saw both edits), so a further compile is a no-op and equals a full rebuild.
    rep = compile_once(store, src)
    assert rep.status == "noop" and state_digest(store) == after_other
    ref = Store(src.parent / "ref.db")
    compile_once(ref, src, strategy="full")
    assert diff_states(canonical_state(ref), canonical_state(store)).equal


def test_lock_is_exclusive_and_waits(tmp_path):
    lock_path = tmp_path / "build.lock"
    with BuildLock(lock_path):
        with pytest.raises(BuildLocked, match="pid"):
            with BuildLock(lock_path, timeout=0):
                pass
    with BuildLock(lock_path, timeout=0):  # released
        pass


def _dakc(*args):
    return [sys.executable, "-m", "dakc", *map(str, args)]


def test_two_processes_compiling_one_workspace(tmp_path):
    write_files(tmp_path / "knowledge", POSTGRES_DOCS)
    Workspace.init(tmp_path, provider="mock")
    assert subprocess.run(_dakc("compile", "-w", tmp_path), capture_output=True).returncode == 0
    write_files(tmp_path / "knowledge", {"databases/redis.md": "Redis supports Clustering. Redis supports Replication.\n"})

    holder = subprocess.Popen(_dakc("compile", "-w", tmp_path, "--fault", "extract:1:pause"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(0.8)  # holder is now paused inside its build, holding the lock
    rejected = subprocess.run(_dakc("compile", "-w", tmp_path), capture_output=True, text=True)
    reader = subprocess.run(_dakc("status", "-w", tmp_path), capture_output=True, text=True)
    waiter = subprocess.Popen(_dakc("compile", "-w", tmp_path, "--wait", "20"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert holder.wait(timeout=30) == 0
    assert waiter.wait(timeout=30) == 0

    assert rejected.returncode == 3 and "another DAKC build is running" in rejected.stderr
    assert reader.returncode == 0, "readers are never blocked by a running build"
    assert "Nothing changed" in waiter.stdout.read(), "the queued build ran after the first and found it up to date"

    store = Store(tmp_path / ".dakc" / "dakc.db")
    assert [b.status for b in store.builds()].count("running") == 0
    ref = Store(tmp_path / "ref.db")
    compile_once(ref, tmp_path / "knowledge", strategy="full")
    assert diff_states(canonical_state(ref), canonical_state(store)).equal
