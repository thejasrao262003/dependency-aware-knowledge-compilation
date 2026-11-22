"""Workspace build lock.

Only one process may compile, roll back or project a workspace at a time.
SQLite already serializes the commit transaction, but two builds that both
*compute* from the same HEAD and then commit one after the other would lose an
update. The lock covers the whole build: recovery, load, compute, commit and
projection. Readers (status, explain, graph, the API) never take it; WAL mode
lets them read a consistent snapshot while a build runs.
"""

from __future__ import annotations

import os
import time
from pathlib import Path


class BuildLocked(RuntimeError):
    """Another process holds the workspace build lock."""


class BuildLock:
    def __init__(self, path: Path, timeout: float = 0.0, poll: float = 0.1):
        self.path = path
        self.timeout = max(0.0, timeout)
        self.poll = poll
        self._fh = None

    def _try_lock(self) -> bool:
        try:
            import fcntl

            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                return False
        except ImportError:  # pragma: no cover - Windows
            import msvcrt

            try:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False

    def _unlock(self) -> None:
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except ImportError:  # pragma: no cover - Windows
            import msvcrt

            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)

    def holder(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip() or "unknown"
        except OSError:
            return "unknown"

    def __enter__(self) -> BuildLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115 - held for the lock's lifetime
        deadline = time.monotonic() + self.timeout
        while not self._try_lock():
            if time.monotonic() >= deadline:
                holder = self.holder()
                self._fh.close()
                self._fh = None
                raise BuildLocked(f"another DAKC build is running on this workspace (pid {holder}); retry or pass --wait SECONDS")
            time.sleep(self.poll)
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return self

    def __exit__(self, *exc) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.flush()
            self._unlock()
        finally:
            self._fh.close()
            self._fh = None


def workspace_lock(store_path: Path, timeout: float = 0.0) -> BuildLock:
    return BuildLock(store_path.parent / "build.lock", timeout)
