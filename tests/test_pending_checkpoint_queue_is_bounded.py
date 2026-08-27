"""One project may not hold back events until `run/state.json` is unreadable.

The pending checkpoint queue is a debounce buffer, and it lives inside
`run/state.json`. A reader refuses that file over 256 KiB: measured on this
vault on 2026-08-26, one session had queued 136 events weighing 184 KiB, the
file reached 262 KiB, and doctor reported that it could check neither the
scheduler nor the captures. Nothing dropped the events, because the state
trimmer only knows the dedupe and reducer maps.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import integration_adapter  # noqa: E402
from project_journal import CheckpointReducer  # noqa: E402

NOW = datetime(2026, 8, 26, 21, 0, tzinfo=timezone.utc)


def _items(count: int) -> list[dict[str, object]]:
    """Events that carry a delta but are never due on time."""
    return [
        {
            "state_key": "llm-wiki:session",
            "has_project_delta": True,
            # Inside the 30-second debounce window, so only the count can
            # make this queue due.
            "occurred_at": (NOW + timedelta(milliseconds=index * 100)).isoformat(),
        }
        for index in range(count)
    ]


def _reducers() -> dict[str, CheckpointReducer]:
    reducer = CheckpointReducer(host_progress_signals=True)
    reducer.last_checkpoint_at = NOW
    return {"llm-wiki:session": reducer}


def test_a_short_queue_still_waits_for_its_debounce_window() -> None:
    index, decision, waiting = integration_adapter._debounce_due(
        _items(5), _reducers()
    )
    assert (index, decision, waiting) == (None, None, True)


def test_the_queue_checkpoints_once_it_would_outgrow_the_state_file() -> None:
    count = integration_adapter.MAX_PENDING_CHECKPOINT_ITEMS
    index, decision, waiting = integration_adapter._debounce_due(
        _items(count), _reducers()
    )
    assert waiting is False
    assert index == count - 1
    assert decision is not None and decision.reason == "debounce_flush"
