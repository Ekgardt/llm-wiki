"""A session processed later than it ended is filed under the day it happened.

See `docs/research/2026-09-17-a-session-is-filed-under-the-day-it-happened.md`.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import episode_consolidation  # noqa: E402
import flush_memory  # noqa: E402
import integration_adapter  # noqa: E402

YESTERDAY = datetime(2026, 9, 16, 22, 30, tzinfo=timezone.utc)
THIS_MORNING = datetime(2026, 9, 17, 9, 0, tzinfo=timezone.utc)


def _record(occurred_at: object) -> dict[str, object]:
    return {
        "event": "session_end",
        "session": "s-1",
        "host": "claude",
        "trigger": "clear",
        "intent_id": "a" * 64,
        "occurred_at": occurred_at,
    }


def _day_of(record: dict[str, object]) -> str:
    return flush_memory._session_time(record, lambda: THIS_MORNING).strftime("%Y-%m-%d")


def test_the_intent_carries_the_moment_the_session_ended():
    envelope = integration_adapter.normalize_event(
        "claude",
        "session_end",
        {"session_id": "s-1", "reason": "clear", "timestamp": YESTERDAY.isoformat()},
    )

    source = integration_adapter._capture_source_record(envelope, "slug", "clear", "text")

    assert source["occurred_at"] == YESTERDAY.isoformat()


def test_a_session_drained_the_next_morning_keeps_its_own_day():
    """Its day, and the same day on a retry — not the day the worker happened to run."""
    days = (_day_of(_record(YESTERDAY.isoformat())), _day_of(_record(YESTERDAY.isoformat())))

    assert days == (YESTERDAY.astimezone().strftime("%Y-%m-%d"),) * 2


def test_an_intent_with_no_time_or_an_unbelievable_one_uses_the_workers_clock():
    far = (THIS_MORNING - timedelta(days=flush_memory.MAX_BACKDATED_CAPTURE_DAYS + 1)).isoformat()
    today = THIS_MORNING.astimezone().strftime("%Y-%m-%d")

    days = tuple(_day_of(_record(value)) for value in (None, "not a time", far))

    assert days == (today, today, today)


def test_the_daily_entry_of_a_late_session_is_written_under_its_own_day():
    record = _record(YESTERDAY.isoformat())
    chosen = flush_memory._session_time(record, lambda: THIS_MORNING)

    plan = flush_memory._capture_operation_plan(record, "minor", "a lesson", chosen)

    assert plan[0]["path"] == f"knowledge/daily/{YESTERDAY.astimezone():%Y-%m-%d}.md"


def _vault_with_records(tmp_path: Path, day: str, count: int) -> Path:
    directory = tmp_path / "knowledge/raw/sessions" / day
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (directory / f"s{index}.md").write_text("---\ntype: raw-source\n---\n# S\n", "utf-8")
    return tmp_path


def test_a_day_that_gained_a_late_record_is_consolidated_once_more(tmp_path):
    day = "2026-09-16"
    vault = _vault_with_records(tmp_path, day, 2)
    covered = {"consolidated_session_days": {day: {"items": 1, "records": 2}}}
    late = {"consolidated_session_days": {day: {"items": 1, "records": 1}}}

    outcomes = (
        episode_consolidation.pending_days(vault, covered, today="2026-09-17"),
        episode_consolidation.pending_days(vault, late, today="2026-09-17"),
        episode_consolidation._skip_reason(vault, day, late),
    )

    assert outcomes == ([], [day], None)
