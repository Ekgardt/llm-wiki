"""The guard that stops a test from writing into the owner's live vault.

This checkout has been the vault itself since the two directories merged on
2026-08-21, so a test that writes knowledge through the pinned `LLM_WIKI_ROOT`
writes into the owner's memory. `tests/conftest.py` snapshots the watched
directories around the session and fails the run when anything changed.

Session records were left out of that watch, and it cost three fixture files:
`knowledge/raw/sessions/2026-08-2{4,5,6}/session-1.md`, written by
`test_flush_classification` while only `flush_memory.STATE_ROOT` was patched.
The writer never raises by contract, so nothing reported the leak. These tests
hold the watch in place.
"""
from __future__ import annotations

from pathlib import Path

from tests import conftest


def _posix(entries: list[str]) -> list[str]:
    """The guard reports paths with the platform's separators (`conftest`
    builds them with `str(path.relative_to(root))`), so on Windows they carry
    backslashes. These tests state expectations in POSIX form and normalize
    the reported side, keeping the guard itself untouched."""
    return [Path(entry).as_posix() for entry in entries]


def _write(root: Path, relative: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("leaked\n", encoding="utf-8")
    return path


def test_a_session_record_written_during_a_run_is_reported(tmp_path):
    before = conftest._knowledge_entries(tmp_path)
    _write(tmp_path, "knowledge/raw/sessions/2026-08-26/session-1.md")

    leaked = conftest._leaked_entries(before, conftest._knowledge_entries(tmp_path))

    assert _posix(leaked) == ["knowledge/raw/sessions/2026-08-26/session-1.md"]


def test_a_session_record_that_was_already_there_is_not_reported(tmp_path):
    _write(tmp_path, "knowledge/raw/sessions/2026-08-26/session-1.md")
    before = conftest._knowledge_entries(tmp_path)

    leaked = conftest._leaked_entries(before, conftest._knowledge_entries(tmp_path))

    assert leaked == []


def test_the_watch_still_covers_notes_and_projects(tmp_path):
    before = conftest._knowledge_entries(tmp_path)
    _write(tmp_path, "knowledge/notes/leaked-page.md")
    _write(tmp_path, "knowledge/projects/leaked-project/journal.md")

    leaked = conftest._leaked_entries(before, conftest._knowledge_entries(tmp_path))

    assert _posix(leaked) == [
        "knowledge/notes/leaked-page.md",
        "knowledge/projects/leaked-project",
    ]


def test_the_daily_log_is_deliberately_not_watched(tmp_path):
    """The live capture appends to today's log continuously; that is not a leak."""
    before = conftest._knowledge_entries(tmp_path)
    _write(tmp_path, "knowledge/daily/2026-08-26.md")

    leaked = conftest._leaked_entries(before, conftest._knowledge_entries(tmp_path))

    assert leaked == []
