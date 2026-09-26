"""A session record gets the same day whichever capture path writes it.

The queue path stamped local time and the detached path UTC, so an evening session
west of UTC was filed under two different days. Research:
`docs/research/2026-09-14-one-day-for-a-session-record.md`.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import flush_memory  # noqa: E402
import session_evidence  # noqa: E402

EVENING_WEST_OF_UTC = datetime(2026, 9, 13, 22, 30, tzinfo=timezone(timedelta(hours=-3)))


def _record_written(monkeypatch, record: dict) -> list[dict]:
    written: list[dict] = []
    monkeypatch.setattr(
        session_evidence,
        "write_session_evidence",
        lambda root, fields, text, **_kwargs: written.append({**fields, "text": text}),
    )
    flush_memory._keep_session_record(record, lambda: EVENING_WEST_OF_UTC)
    return written


def test_the_worker_files_an_evening_session_under_the_local_day(monkeypatch):
    """The detached path this pinned was retired on 2026-09-25; the worker keeps the rule."""
    record = {"session": "s1", "evidence": [{"parts": [{"text": "hi"}]}]}

    written = _record_written(monkeypatch, record)

    assert session_evidence._capture_day(written[0]) == "2026-09-13"


def test_the_worker_keeps_the_record_before_any_classification(monkeypatch):
    """Retention does not wait for the tier: the record is written from the intent alone."""
    record = {"session": "s1", "evidence": [{"parts": [{"text": "a session"}]}]}

    written = _record_written(monkeypatch, record)

    assert (len(written), written[0]["text"]) == (1, "a session")
