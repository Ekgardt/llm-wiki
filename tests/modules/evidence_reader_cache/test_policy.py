"""Existing cache policy tests, retained without semantic changes."""
from pathlib import Path
from types import SimpleNamespace

import evidence_reader_cache
import pytest


class _FakeReader:
    def __init__(self, path: Path) -> None:
        self.database_path = path
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _scope(root: Path):
    return SimpleNamespace(
        checkout_root=str(root), git_common_dir=None, same_repository=lambda _other: True
    )


def _lease_for(key: str, tmp_path: Path, readers: dict) -> evidence_reader_cache.GraphLease:
    return evidence_reader_cache.leased_graph(
        (key, False),
        catalog_path=tmp_path / "catalog.sqlite3",
        resolve_scope=lambda: _scope(tmp_path),
        open_graph=lambda _scope: readers.setdefault(key, _FakeReader(tmp_path / key)),
    )


def test_eviction_beyond_the_bound_closes_the_oldest_unheld_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence_reader_cache, "MAX_ENTRIES", 1)
    readers: dict[str, _FakeReader] = {}

    _lease_for("first", tmp_path, readers).close()
    _lease_for("second", tmp_path, readers).close()

    assert (readers["first"].closed, readers["second"].closed) == (True, False)


def test_a_held_lease_defers_the_close_until_it_is_released(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence_reader_cache, "MAX_ENTRIES", 1)
    readers: dict[str, _FakeReader] = {}

    held = _lease_for("first", tmp_path, readers)
    _lease_for("second", tmp_path, readers).close()
    still_open = not readers["first"].closed
    held.close()

    assert (still_open, readers["first"].closed) == (True, True)


def test_an_idle_reader_is_closed_on_the_next_access_and_a_held_one_is_not(
    tmp_path, monkeypatch
):
    # The cache's own clock, so the expectation is stated in seconds and not
    # in the granularity of the runner's monotonic clock (15.6 ms on Windows
    # before Python 3.13).
    clock = [0.0]
    monkeypatch.setattr(evidence_reader_cache, "_now", lambda: clock[0])
    readers: dict[str, _FakeReader] = {}
    _lease_for("idle", tmp_path, readers).close()
    held = _lease_for("held", tmp_path, readers)
    clock[0] = evidence_reader_cache.IDLE_SECONDS + 1.0

    _lease_for("fresh", tmp_path, readers).close()
    idle_closed, held_still_open = readers["idle"].closed, not readers["held"].closed
    held.close()

    assert (idle_closed, held_still_open, readers["held"].closed) == (True, True, True)


def test_a_missing_generation_is_not_cached(tmp_path):
    answer = evidence_reader_cache.leased_graph(
        ("absent", False),
        catalog_path=tmp_path / "catalog.sqlite3",
        resolve_scope=lambda: _scope(tmp_path),
        open_graph=lambda _scope: None,
    )

    assert (answer, evidence_reader_cache.cached_entries()) == (None, 0)


@pytest.mark.parametrize(("threadsafety", "supported"), [(0, False), (1, True), (3, True)])
def test_every_build_but_a_single_thread_one_may_lend_a_reader(
    monkeypatch, threadsafety, supported
):
    # Python 3.10 reports a hard-coded 1; the lease lock makes one thread at
    # a time the only requirement, which a multi-thread build already meets.
    monkeypatch.setattr(evidence_reader_cache.sqlite3, "threadsafety", threadsafety)

    assert evidence_reader_cache.shared_readers_supported() is supported
