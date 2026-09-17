"""Session start shows what a daily entry says, not the labels the worker wrote above it.

See `docs/research/2026-09-17-the-daily-excerpt-shows-the-entry-not-its-label.md`.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import flush_memory  # noqa: E402
import session_start_context  # noqa: E402

BODY = "**Decisions made**\n- chose the queue over the file\n- kept the old reader"
CHOSEN_AT = datetime(2026, 9, 17, 10, 0, 0, tzinfo=timezone.utc)


def _worker_block(event: str) -> list[str]:
    """The entry exactly as the capture worker writes it."""
    record = {
        "event": event,
        "session": "0b8e1f0c-session",
        "trigger": "other",
        "host": "claude",
        "intent_id": "a" * 64,
    }
    return flush_memory._capture_daily_block(record, "major", BODY, CHOSEN_AT).splitlines()


@pytest.mark.parametrize("event", ["session_end", "pre_compact"])
def test_the_excerpt_is_the_header_and_the_entry(event):
    cleaned = session_start_context.clean_block(_worker_block(event))

    assert cleaned == [
        f"## [10:00:00] {event.replace('_', '-')}",
        "**Decisions made**",
        "- chose the queue over the file",
        "- kept the old reader",
    ]
