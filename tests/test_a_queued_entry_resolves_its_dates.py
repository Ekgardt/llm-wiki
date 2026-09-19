"""An entry written by the capture worker resolves the dates it mentions.

See `docs/research/2026-09-17-a-queued-entry-resolves-its-dates-too.md`.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import flush_memory  # noqa: E402
import operational_ownership  # noqa: E402

from tests.test_capture_terminal import (  # noqa: E402
    _FakeNoContentProvider,
    _ready_intent_binding,
    _TimedCaptureProcessor,
)
from tests.test_queue_v3_capture_links import _coordinator, _queue  # noqa: E402

# A Sunday, so "last Thursday" is the 13th.
CHOSEN_AT = datetime(2026, 8, 16, 12, 34, 56, tzinfo=timezone.utc)
WIRE_OUTPUT = "FLUSH_MAJOR\n- **Decisions made** - I met her last Thursday."
INTENT = {
    "event": "session_end",
    "session": "session-1",
    "trigger": "other",
    "host": "claude",
    "intent_id": "a" * 64,
}


def test_the_worker_writes_the_resolved_date_under_its_entry(tmp_path: Path) -> None:
    (tmp_path / "knowledge" / "daily").mkdir(parents=True)
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    _ready_intent_binding(queue, coordinator, registry, "decision evidence")
    processor = _TimedCaptureProcessor(
        queue, coordinator, _FakeNoContentProvider(WIRE_OUTPUT), CHOSEN_AT
    )

    flush_memory.run_capture_worker_once(queue, coordinator, process_missing=processor)

    written = (tmp_path / "knowledge" / "daily" / "2026-08-16.md").read_text("utf-8")
    assert "2026-08-13 — " in written


def _decision(dated: bool) -> dict:
    tier, body = flush_memory._parse_capture_wire_output(WIRE_OUTPUT)
    plan = flush_memory._capture_operation_plan(INTENT, tier, body, CHOSEN_AT, dated=dated)
    return {
        "wire_output": WIRE_OUTPUT,
        "tier": tier,
        "outcome": "major_written",
        "operation_plan": plan,
    }


def test_a_decision_stored_before_the_dates_were_resolved_is_still_valid() -> None:
    stored, fresh = _decision(dated=False), _decision(dated=True)

    flush_memory._require_capture_decision_semantics(stored, INTENT)
    flush_memory._require_capture_decision_semantics(fresh, INTENT)

    assert stored["operation_plan"] != fresh["operation_plan"]
