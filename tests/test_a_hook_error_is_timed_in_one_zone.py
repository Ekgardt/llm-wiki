"""A hook error is judged by the moment it happened, whatever the zone (audit 2026-09-26 B-25).

docs/research/2026-09-26-a-hook-error-is-timed-in-one-zone.md
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import doctor
import integration_adapter
import pytest


@pytest.fixture()
def new_york(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


pytestmark = pytest.mark.skipif(not hasattr(time, "tzset"), reason="the zone is set through tzset")


def test_a_line_written_now_without_an_offset_is_live(new_york) -> None:
    written = datetime.now().isoformat(timespec="seconds")

    assert doctor._hook_error_is_live(written, datetime.now(timezone.utc)) is True


def test_a_line_written_now_carries_its_offset(new_york, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(integration_adapter, "STATE_ROOT", tmp_path)

    integration_adapter._log_checkpoint_error(RuntimeError("boom"))

    stamp = (tmp_path / "logs" / "hook-errors.log").read_text(encoding="utf-8").split("]")[0][1:]
    assert datetime.fromisoformat(stamp).utcoffset() is not None
