"""The pending-intent pass looks past records it cannot finish, as adoption does (audit 2026-09-27 C-4).

docs/research/2026-09-27-both-recovery-passes-look-past-a-bad-head.md
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from tests.a_publication_that_stopped_half_way_is_finished_helpers import (
    LATER,
    _complete,
    _publish_pending_intent,
)
from tests.capture_intent_adoption_helpers import _coordinator, _queue


def test_a_head_of_broken_pending_records_does_not_hide_the_intact_one(tmp_path: Path, monkeypatch) -> None:
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    halves = [_publish_pending_intent(tmp_path, queue, coordinator, f"half-{index}".encode()) for index in range(4)]
    offered = queue.pending_capture_intents(4)
    for record in offered[:-1]:
        (tmp_path / str(record["relative_path"])).unlink()

    result = _complete(queue, coordinator, tmp_path, limit=2, now=LATER)

    completed = [entry["intent_id"] for entry in result["completed"]]
    assert (completed, len(result["skipped"]), len(halves)) == ([str(offered[-1]["intent_id"])], 3, 4)


def test_the_cutoff_is_written_in_the_queue_s_own_width() -> None:
    import capture_adoption

    whole_second = datetime(2026, 9, 27, 10, 0, 0, tzinfo=UTC)

    assert capture_adoption.stale_pending_cutoff(whole_second) == "2026-09-27T09:59:00.000000+00:00"
