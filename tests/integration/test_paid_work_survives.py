"""A provider call that fails during extraction must not throw away the calls that succeeded.

Regression: provider results were collected for the whole stage before any was cached, so
one failing call (after the client's own retries) discarded every paid extraction, and the
next attempt paid for all of them again.
"""

from __future__ import annotations

import threading

import pytest

from dakc.compiler import Compiler
from dakc.providers import make_provider
from dakc.storage import Store

from tests.conftest import write_files


class FlakyExtractor:
    """Behaves like a paid LLM extractor (run concurrently) and fails on one passage."""

    is_llm, free = True, False

    def __init__(self, inner, fail_on: str | None):
        self.inner, self.fail_on = inner, fail_on
        self.fingerprint = inner.fingerprint
        self.lock = threading.Lock()
        self.ok: list[str] = []

    def extract(self, text, targets=None):
        if self.fail_on and self.fail_on in text:
            raise RuntimeError("provider unavailable")
        res = self.inner.extract(text, targets)
        with self.lock:
            self.ok.append(text)
        return res


def bundle(fail_on: str | None) -> tuple:
    b = make_provider("mock")
    flaky = FlakyExtractor(b.extractor, fail_on)
    b.extractor = flaky
    return b, flaky


def test_a_failed_extraction_keeps_every_paid_result_and_the_retry_pays_only_for_the_rest(tmp_path):
    src = tmp_path / "knowledge"
    write_files(src, {f"doc{i:02d}.md": f"# Doc {i}\n\nService{i:02d} supports Feature{i:02d}.\n" for i in range(40)})
    store = Store(tmp_path / "s.db")

    b1, first = bundle(fail_on="Service05")
    with pytest.raises(RuntimeError, match="provider unavailable"):
        Compiler(store, b1, src, jobs=8, acquire_lock=False).run()
    assert store.head == 0  # nothing committed
    cached = store.conn.execute("SELECT COUNT(*) FROM extraction_cache").fetchone()[0]
    assert cached == len(first.ok) and cached >= 1  # every call that succeeded was kept

    b2, second = bundle(fail_on=None)
    report, _ = Compiler(store, b2, src, jobs=8, acquire_lock=False).run()
    assert report.status == "committed"
    assert not set(second.ok) & set(first.ok)  # nothing paid twice
    assert len(second.ok) == 40 - cached and report.usage.cache_hits == cached
    store.close()
