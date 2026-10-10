"""Real source archival keeps logical evidence readable and correctly typed."""
from __future__ import annotations

from datetime import date
from pathlib import PurePosixPath

import archive_sessions
import breadcrumb_evidence as evidence
import episode_consolidation
import flush_memory
import markdown_transaction
import pytest

from tests.test_breadcrumb_terminal_proof import _committed, _complete, _delivery


def _archive_sources(root, coordinator, monkeypatch):
    sessions = root / "knowledge/raw/sessions"
    monkeypatch.setattr(archive_sessions, "ROOT", root)
    monkeypatch.setattr(archive_sessions, "SESSIONS", sessions)
    monkeypatch.setattr(archive_sessions, "ARCHIVE", sessions / "archive")
    monkeypatch.setattr(markdown_transaction, "_default_coordinator", lambda: coordinator)
    results = archive_sessions.archive_sessions(days=90, today=date(2027, 2, 1), apply=True)
    assert results and all(result.startswith("ARCHIVED:") for result in results)


def _archived_name(relative):
    path = PurePosixPath(relative)
    day = path.parent.name
    return f"knowledge/raw/sessions/archive/{day[:7]}/{day}/{path.name}"


def test_actual_session_archival_preserves_terminal_proof(tmp_path, monkeypatch):
    with _delivery(tmp_path) as arguments:
        transaction = _committed(arguments)
        _archive_sources(tmp_path, arguments[1], monkeypatch)
        head = arguments[-1]["source"]["path"]

        assert not (tmp_path / head).exists()
        assert evidence.read_permanent_source(tmp_path, head) == arguments[6].content
        disposition = flush_memory._capture_markdown_disposition(transaction, arguments[7])
        assert _complete(arguments, disposition)


def test_partial_archive_can_read_parts_in_both_locations(tmp_path, monkeypatch):
    with _delivery(tmp_path) as arguments:
        _committed(arguments)
        _archive_sources(tmp_path, arguments[1], monkeypatch)
        head = arguments[-1]["source"]["path"]
        target = tmp_path / head
        target.parent.mkdir(parents=True)
        (tmp_path / _archived_name(head)).rename(target)

        assert evidence.read_permanent_source(tmp_path, head) == arguments[6].content


def test_present_conflicting_head_cannot_fall_back_to_archived_copy(tmp_path, monkeypatch):
    with _delivery(tmp_path) as arguments:
        _committed(arguments)
        _archive_sources(tmp_path, arguments[1], monkeypatch)
        head = arguments[-1]["source"]["path"]
        target = tmp_path / head
        target.parent.mkdir(parents=True)
        target.write_bytes(b"conflicting source")

        with pytest.raises(ValueError):
            evidence.read_permanent_source(tmp_path, head)


def test_integrity_fragments_are_not_classified_as_independent_sessions(tmp_path):
    with _delivery(tmp_path) as arguments:
        _committed(arguments)
        directory = tmp_path / PurePosixPath(arguments[-1]["source"]["path"]).parent
        session = directory / "real-session.md"
        session.write_text("---\ntype: raw-source\nsession: real\n---\nFull conversation\n")

        assert episode_consolidation.session_records(tmp_path, "2026-09-29") == [session]
