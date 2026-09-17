"""The adoption pass looks past intents it cannot adopt, and the worker names them.

See `docs/research/2026-09-17-the-adoption-pass-says-what-it-skipped-and-looks-past-it.md`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import capture_adoption  # noqa: E402
import capture_diagnostics  # noqa: E402
import flush_memory  # noqa: E402
import memory_state  # noqa: E402

from tests.test_capture_intent_adoption import (  # noqa: E402
    _coordinator,
    _publish_ready_intent,
    _queue,
)


def _orphans_with_only_the_newest_intact(tmp_path: Path, queue, coordinator, count: int) -> str:
    """Publish `count` orphans, delete every record but the last the queue offers."""
    for index in range(count):
        _publish_ready_intent(tmp_path, queue, coordinator, f"orphan-{index}".encode())
    offered = queue.ready_capture_intents_without_task(count)
    for record in offered[:-1]:
        (tmp_path / str(record["relative_path"])).unlink()
    return str(offered[-1]["intent_id"])


def test_a_window_full_of_lost_records_does_not_hide_the_orphan_behind_it(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    intact = _orphans_with_only_the_newest_intact(tmp_path, queue, coordinator, 4)

    result = capture_adoption.adopt_orphaned_capture_intents(
        queue, coordinator, state_root=tmp_path, limit=2
    )

    adopted = [entry["intent_id"] for entry in result["adopted"]]
    assert (adopted, len(result["skipped"])) == ([intact], 3)


def test_the_worker_names_an_intent_it_could_not_adopt(tmp_path: Path, monkeypatch) -> None:
    run = tmp_path / "diagnostics"
    monkeypatch.setattr(memory_state, "STATE_DIR", run)
    monkeypatch.setattr(memory_state, "STATE_FILE", run / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", run / "state.json.lock")
    monkeypatch.setattr(capture_diagnostics, "FAILURE_LOG", run / "capture-failures.jsonl")
    monkeypatch.setattr(flush_memory, "STATE_ROOT", tmp_path)
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    lost = _publish_ready_intent(tmp_path, queue, coordinator, b"lost-record")
    (tmp_path / str(lost["ready"])).unlink()

    flush_memory._adopt_orphaned_intents(queue, coordinator)

    trail = (run / "capture-failures.jsonl").read_text("utf-8").splitlines()
    records = [json.loads(line) for line in trail]
    assert [record["kind"] for record in records] == ["capture_adoption"]
    assert lost["intent_id"] in records[0]["reason"]
