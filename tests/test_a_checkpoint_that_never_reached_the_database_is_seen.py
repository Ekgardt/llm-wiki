"""A checkpoint still queued after a nightly drain is reported (audit 2026-09-26 C-12).

docs/research/2026-09-26-a-checkpoint-that-never-reached-the-database-is-seen.md
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import doctor
import pytest

NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)


def _queue(state_root: Path, occurred_at: datetime) -> None:
    run = state_root / "run"
    run.mkdir(parents=True)
    event = {"event_id": "e-1", "occurred_at": occurred_at.isoformat()}
    (run / "state.json").write_text(json.dumps({"project_checkpoint_pending": {"demo": [event]}}), encoding="utf-8")


@pytest.mark.parametrize(("age", "status"), [(timedelta(days=2), "degraded"), (timedelta(hours=1), "ok")])
def test_a_queued_checkpoint_is_judged_by_its_age(tmp_path: Path, age: timedelta, status: str) -> None:
    _queue(tmp_path, NOW - age)

    finding = doctor._checkpoint_check(tmp_path, NOW)

    assert finding["status"] == status


def test_the_finding_names_the_project_whose_events_wait(tmp_path: Path) -> None:
    _queue(tmp_path, NOW - timedelta(days=2))

    assert "demo" in doctor._checkpoint_check(tmp_path, NOW)["message"]
