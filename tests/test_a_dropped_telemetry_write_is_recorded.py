"""A telemetry write that failed leaves a diagnostic record.

`best_effort_record_events` returned False on any error and every caller ignored
it, so a broken telemetry database was invisible. See
docs/research/2026-09-25-a-dropped-telemetry-write-is-recorded.md.
"""

from __future__ import annotations

from pathlib import Path

import retrieval_telemetry


def _event():
    return retrieval_telemetry.make_event(
        event_kind="impression",
        retrieval_mode="HYBRID",
        candidate_id="page",
        rank=1,
        generation="generation-test",
        source_tool="test",
        query="where is the page",
    )


def test_a_failed_write_is_recorded_as_a_telemetry_event(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "capture_diagnostics.record_capture_failure",
        lambda kind, reason, **_fields: calls.append((kind, reason)),
    )

    written = retrieval_telemetry.best_effort_record_events([_event()], db_path=tmp_path)

    assert (written, [kind for kind, _reason in calls]) == (False, ["telemetry_event"])


def test_a_written_event_records_nothing(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "capture_diagnostics.record_capture_failure", lambda kind, *_a, **_k: calls.append(kind)
    )

    written = retrieval_telemetry.best_effort_record_events(
        [_event()], db_path=tmp_path / "telemetry.sqlite3"
    )

    assert (written, calls) == (True, [])
