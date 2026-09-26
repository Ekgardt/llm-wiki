"""A long transcript's head and tail are its turns, not service records (audit 2026-09-26 C-5).

docs/research/2026-09-26-a-long-session-keeps-its-first-turns.md
"""
from __future__ import annotations

import json
from pathlib import Path

import integration_adapter
from session_evidence import is_service_record, render_transcript

LIMIT = 64 * 1024


def _line(entry: dict) -> str:
    return json.dumps(entry) + "\n"


def _turn(role: str, text: str) -> str:
    return _line({"type": role, "message": {"content": [{"type": "text", "text": text}]}})


def _snapshots(count: int) -> str:
    snapshot = {"type": "file-history-snapshot", "snapshot": {"files": "x" * 900}}
    return _line(snapshot) * count


def _transcript(tmp_path: Path) -> Path:
    body = (
        _snapshots(200)
        + _turn("user", "FIRST QUESTION")
        + "".join(_turn("assistant", f"middle {index} " + "y" * 900) for index in range(400))
        + _turn("assistant", "LAST ANSWER")
        + _snapshots(200)
    )
    path = tmp_path / "session.jsonl"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_first_and_last_turns_survive_a_head_and_tail_of_service_records(tmp_path) -> None:
    text = integration_adapter._capture_transcript_text(_transcript(tmp_path), LIMIT)
    rendered = render_transcript(text)

    assert ("FIRST QUESTION" in rendered, "LAST ANSWER" in rendered, len(text) <= LIMIT + 512) == (
        True,
        True,
        True,
    )


def test_a_plain_text_line_is_not_a_service_record() -> None:
    kinds = [
        is_service_record(_snapshots(1).strip()),
        is_service_record(_turn("user", "hi").strip()),
        is_service_record("a plain log line"),
    ]

    assert kinds == [True, False, False]
