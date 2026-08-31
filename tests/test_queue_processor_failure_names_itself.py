"""A failed queue processor leaves its reason on disk, not just a code.

Measured 2026-08-28 on the live vault: `attempt_history` held 72
`processor_failed` rows and eleven `flush` tasks stuck at eight attempts
each, and no reason existed anywhere — the worker runs each processor in a
child and `_processor_result_frame` deliberately flattens any exception to
one byte so no traceback crosses the pipe. That contract is right; losing the
reason with it was not. Eleven sessions had failed to become memory and the
trail could not say why.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import memory_queue  # noqa: E402

TASK = {"id": "a" * 32, "kind": "flush", "payload": {}}


def _boom(_task):
    raise RuntimeError("the intent could not be read")


def _trail(log: Path) -> list[dict]:
    if not log.exists():
        return []
    lines = log.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`capture_diagnostics` binds its paths at import, so bind them here."""
    import capture_diagnostics

    reports = tmp_path / "logs"
    reports.mkdir(parents=True, exist_ok=True)
    log = reports / "capture-failures.jsonl"
    monkeypatch.setattr(capture_diagnostics, "REPORTS_DIR", reports)
    monkeypatch.setattr(capture_diagnostics, "FAILURE_LOG", log)
    return log


def test_the_frame_still_carries_one_stable_code(isolated: Path) -> None:
    """The wire contract does not change: no traceback crosses the pipe."""
    assert memory_queue._processor_result_frame(_boom, TASK) == b"E"


def test_the_reason_reaches_the_trail(isolated: Path) -> None:
    memory_queue._processor_result_frame(_boom, TASK)
    reasons = [row["reason"] for row in _trail(isolated)]
    assert any("the intent could not be read" in reason for reason in reasons)


def test_the_trail_line_names_the_task_kind(isolated: Path) -> None:
    memory_queue._processor_result_frame(_boom, TASK)
    reasons = [row["reason"] for row in _trail(isolated)]
    assert any(reason.startswith("flush: RuntimeError") for reason in reasons)


def test_a_broken_trail_never_changes_the_outcome(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Diagnostics are best effort; the processor's verdict is not."""

    def _explode(*_args, **_kwargs):
        raise OSError("no room for diagnostics")

    monkeypatch.setattr(
        "capture_diagnostics.record_capture_failure", _explode, raising=True
    )
    assert memory_queue._processor_result_frame(_boom, TASK) == b"E"


def test_a_success_writes_nothing(isolated: Path) -> None:
    assert memory_queue._processor_result_frame(lambda _task: True, TASK) == b"T"
    assert _trail(isolated) == []
