"""Failures recorded at the same moment all reach the trail, and the trail stays bounded.

See `docs/research/2026-09-17-two-failures-at-once-both-reach-the-trail.md`.
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import capture_diagnostics  # noqa: E402
import memory_state  # noqa: E402

WRITERS = 12
FAILURES_EACH = 10


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


def _fail_repeatedly(writer: int) -> None:
    for index in range(FAILURES_EACH):
        capture_diagnostics.record_capture_failure("burst", f"writer {writer} failure {index}")


def _reasons(path: Path) -> list[str]:
    return [json.loads(line)["reason"] for line in path.read_text("utf-8").splitlines()]


def test_every_line_of_a_burst_is_kept_whole(trail):
    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        list(pool.map(_fail_repeatedly, range(WRITERS)))

    expected = {f"writer {w} failure {i}" for w in range(WRITERS) for i in range(FAILURES_EACH)}
    assert set(_reasons(trail)) == expected


def test_an_overgrown_trail_is_cut_below_its_cap_and_keeps_the_newest(trail):
    old = json.dumps({"kind": "old", "reason": "x" * 180})
    trail.write_text((old + "\n") * 1500, encoding="utf-8")

    capture_diagnostics.record_capture_failure("fresh", "the newest failure")

    assert trail.stat().st_size <= capture_diagnostics.MAX_FAILURE_LOG_BYTES * 3 // 4 + 1
    assert _reasons(trail)[-1] == "the newest failure"
