"""A session record gets the same day whichever capture path writes it.

The queue path stamped local time and the detached path UTC, so an evening session
west of UTC was filed under two different days. Research:
`docs/research/2026-09-14-one-day-for-a-session-record.md`.
"""
from __future__ import annotations

import json
import sys
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import flush_memory  # noqa: E402
import session_evidence  # noqa: E402

EVENING_WEST_OF_UTC = datetime(2026, 9, 13, 22, 30, tzinfo=timezone(timedelta(hours=-3)))


def test_the_detached_path_files_an_evening_session_under_the_local_day(tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    turn = json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n"
    written: list[dict] = []
    # The transcript allowlist refuses a temporary path; the text is what matters here.
    monkeypatch.setattr(flush_memory, "read_transcript_tail", lambda path, max_chars: turn)
    monkeypatch.setattr(flush_memory, "_capture_now", lambda: EVENING_WEST_OF_UTC)
    monkeypatch.setattr(session_evidence, "write_session_evidence", lambda root, fields, text: written.append(dict(fields)))
    arguments = Namespace(transcript=str(transcript), session_id="s1", agent="claude", event="SessionEnd", source_event_id=None)

    flush_memory._keep_transcript_record(arguments)

    assert session_evidence._capture_day(written[0]) == "2026-09-13"
