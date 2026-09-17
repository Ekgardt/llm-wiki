"""A trim of the failure trail never crosses an append, so no reason is erased.

See `docs/research/2026-09-17-a-trim-never-drops-a-line-it-did-not-read.md`.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import capture_diagnostics  # noqa: E402
import memory_state  # noqa: E402

from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402

OVERSIZE_LINES = 1500


@pytest.fixture
def trail(tmp_path, monkeypatch) -> Path:
    run = tmp_path / "run"
    monkeypatch.setattr(memory_state, "STATE_DIR", run)
    monkeypatch.setattr(memory_state, "STATE_FILE", run / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", run / "state.json.lock")
    path = tmp_path / "logs" / "capture-failures.jsonl"
    path.parent.mkdir()
    monkeypatch.setattr(capture_diagnostics, "FAILURE_LOG", path)
    return path


def _oversize(path: Path) -> int:
    line = json.dumps({"kind": "old", "reason": "x" * 180})
    path.write_text((line + "\n") * OVERSIZE_LINES, encoding="utf-8")
    return path.stat().st_size


def _reasons(path: Path) -> list[str]:
    return [json.loads(line)["reason"] for line in path.read_text("utf-8").splitlines()]


def _appending_writer(reason: str) -> threading.Thread:
    record = {"kind": "burst", "reason": reason, "at": "now", "outcome": "lost"}
    worker = threading.Thread(target=capture_diagnostics._append_failure_line, args=(record,))
    worker.start()
    return worker


def test_a_line_appended_during_a_trim_is_not_erased_by_it(trail, monkeypatch):
    """The trim reads every line and writes the file back; an append must not fall in between.

    The writer starts after the trim has read the trail and before it replaces it —
    the window the whole-file rewrite used to lose a line in. `join(0.05)` is the
    test's own pause: it expects the writer to still be waiting for the lock.
    """
    _oversize(trail)
    trimmed_tail = capture_diagnostics._trimmed_tail
    writers: list[threading.Thread] = []

    def _append_after_reading(lines: list[str], max_bytes: int) -> list[str]:
        kept = trimmed_tail(lines, max_bytes)
        writers.append(_appending_writer("appended during the trim"))
        writers[-1].join(0.05)
        return kept

    monkeypatch.setattr(capture_diagnostics, "_trimmed_tail", _append_after_reading)
    capture_diagnostics._trim_failure_log()
    writers[-1].join(SHORT_TIMEOUT)

    assert "appended during the trim" in _reasons(trail)


def test_a_trim_leaves_the_trail_alone_while_another_writer_holds_its_lock(trail):
    """The writer holding the lock may be appending; its line must survive."""
    size = _oversize(trail)

    with capture_diagnostics.trail_lock() as held:
        capture_diagnostics._trim_failure_log()
        after = (held, trail.stat().st_size)

    assert after == (True, size)


def test_a_line_is_still_recorded_while_the_trail_lock_is_held(trail):
    """A held lock delays a line; it never loses one."""
    trail.write_text("", encoding="utf-8")

    with capture_diagnostics.trail_lock() as held:
        capture_diagnostics.record_capture_failure("held", "a failure during a trim")

    assert (held, _reasons(trail)) == (True, ["a failure during a trim"])
