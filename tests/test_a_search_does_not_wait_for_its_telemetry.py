"""A search returns without waiting for its telemetry to reach the disk.

Each telemetry commit flushes a rollback journal with `synchronous=FULL`, and
those flushes cost a search 40-220 ms (measured 2026-11-27). The writer thread
waits for them instead; the events still arrive.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import retrieval  # noqa: E402
import retrieval_telemetry  # noqa: E402

PAGE = "knowledge/notes/auth.md"


def test_impressions_are_written_after_the_search_returns(tmp_path, monkeypatch) -> None:
    disk = threading.Event()
    written: list = []

    def slow_record(events, **_bounds):
        disk.wait(30)
        written.extend(events)
        return len(events)

    monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", tmp_path / "telemetry.sqlite3")
    monkeypatch.setattr(retrieval_telemetry, "record_events", slow_record)
    search = threading.Thread(
        target=retrieval._record_impressions,
        args=([{"path": PAGE, "effective_mode": "generation", "generation": "g1"}],),
        kwargs={"query": "who signs in", "corpus_generation": "g1", "source_tool": "search_memory"},
    )
    search.start()
    search.join(10)
    returned_first = not search.is_alive()
    disk.set()
    search.join()
    retrieval_telemetry.flush_events_later()

    assert returned_first
    assert [event.candidate_id for event in written] == [PAGE]


def test_a_flush_waits_for_every_batch(tmp_path, monkeypatch) -> None:
    written: list = []
    monkeypatch.setattr(
        retrieval_telemetry, "record_events", lambda events, **_: written.extend(events)
    )
    event = retrieval_telemetry.best_effort_make_event(
        event_kind="impression", query="q", retrieval_mode="base",
        candidate_id=PAGE, rank=1, generation="g1", source_tool="test",
    )

    for _ in range(3):
        retrieval_telemetry.record_events_later([event])
    retrieval_telemetry.flush_events_later()

    assert len(written) == 3
