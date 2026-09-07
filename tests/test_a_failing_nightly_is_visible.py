"""The nightly failed for five days and every session start said nothing.

The health block runs the doctor under a 0.1 second budget while the checks want
1.77 seconds, so it always reports that nothing was measured. That budget is
deliberate — session start must not wait on the doctor — but it left the one
place a degraded vault could surface saying only that it had not looked.

Measured 2026-09-05: the nightly had failed every night since 2026-08-30, no
evidence generation was ever activated, and every answer for five days came from
the lexical leg alone with complete vectors unreachable on disk.
`run/state.json` had carried `last_nightly_status: failed` the whole time.

Reading one field costs microseconds.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import session_start_context  # noqa: E402


def _state(tmp_path: Path, monkeypatch, payload: object) -> None:
    run = tmp_path / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "state.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(session_start_context, "STATE_ROOT", tmp_path)


def test_a_failing_nightly_is_named_with_its_last_success(tmp_path, monkeypatch) -> None:
    _state(
        tmp_path,
        monkeypatch,
        {"last_nightly_status": "failed", "last_nightly_date": "2026-08-30"},
    )

    line = session_start_context._nightly_line()

    assert "nightly maintenance is failing" in line
    assert "2026-08-30" in line


def test_a_nightly_that_never_ran_still_names_the_failure(tmp_path, monkeypatch) -> None:
    _state(tmp_path, monkeypatch, {"last_nightly_status": "failed"})

    assert "never" in session_start_context._nightly_line()


def test_a_healthy_nightly_says_nothing(tmp_path, monkeypatch) -> None:
    """A block that speaks when there is nothing to say stops being read."""
    _state(
        tmp_path,
        monkeypatch,
        {"last_nightly_status": "success", "last_nightly_date": "2026-09-05"},
    )

    assert session_start_context._nightly_line() == ""


def test_unreadable_state_says_nothing_rather_than_guessing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(session_start_context, "STATE_ROOT", tmp_path / "absent")

    assert session_start_context._nightly_line() == ""


def test_the_unmeasured_block_carries_the_nightly_line(tmp_path, monkeypatch) -> None:
    _state(
        tmp_path,
        monkeypatch,
        {"last_nightly_status": "failed", "last_nightly_date": "2026-08-30"},
    )

    block = session_start_context._unmeasured_health_block(9, 18)

    assert "nightly maintenance is failing" in block
    assert "9 of 18 checks" in block
