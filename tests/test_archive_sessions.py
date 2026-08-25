"""Session records age out of the active tree without ever being deleted."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _vault_with_sessions(tmp_path: Path, days: dict[str, str]) -> Path:
    vault = tmp_path / "vault"
    sessions = vault / "knowledge/raw/sessions"
    for day, text in days.items():
        directory = sessions / day
        directory.mkdir(parents=True)
        (directory / "session-1.md").write_text(text, encoding="utf-8")
    return vault


def _point_at(module, monkeypatch, vault: Path) -> None:
    sessions = vault / "knowledge/raw/sessions"
    monkeypatch.setattr(module, "ROOT", vault)
    monkeypatch.setattr(module, "SESSIONS", sessions)
    monkeypatch.setattr(module, "ARCHIVE", sessions / "archive")
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(vault.parent / "state"))


def test_only_days_past_the_window_are_named(tmp_path, monkeypatch):
    import archive_sessions

    vault = _vault_with_sessions(
        tmp_path, {"2026-01-01": "old", "2026-08-20": "recent"}
    )
    _point_at(archive_sessions, monkeypatch, vault)

    planned = archive_sessions.archive_sessions(
        days=90, today=date(2026, 8, 25), apply=False
    )

    assert planned == ["WOULD ARCHIVE: knowledge/raw/sessions/2026-01-01/session-1.md"]
    assert (vault / "knowledge/raw/sessions/2026-01-01/session-1.md").exists()


def test_an_archived_record_keeps_its_bytes_one_directory_deeper(tmp_path, monkeypatch):
    import archive_sessions

    vault = _vault_with_sessions(tmp_path, {"2026-01-01": "the record"})
    _point_at(archive_sessions, monkeypatch, vault)

    outcomes = archive_sessions.archive_sessions(
        days=90, today=date(2026, 8, 25), apply=True
    )

    archived = vault / "knowledge/raw/sessions/archive/2026-01/2026-01-01/session-1.md"
    assert outcomes == ["ARCHIVED: knowledge/raw/sessions/2026-01-01/session-1.md"]
    assert archived.read_text(encoding="utf-8") == "the record"
    assert not (vault / "knowledge/raw/sessions/2026-01-01").exists()


def test_a_directory_that_is_not_a_day_is_left_alone(tmp_path, monkeypatch):
    import archive_sessions

    vault = _vault_with_sessions(tmp_path, {"2026-01-01": "old"})
    (vault / "knowledge/raw/sessions/archive").mkdir()
    (vault / "knowledge/raw/sessions/notes").mkdir()
    _point_at(archive_sessions, monkeypatch, vault)

    stale = archive_sessions.aged_out(
        vault / "knowledge/raw/sessions", date(2026, 8, 25)
    )

    assert [day.name for day in stale] == ["2026-01-01"]
