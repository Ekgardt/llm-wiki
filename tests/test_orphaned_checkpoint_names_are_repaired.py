"""A checkpoint no request can name again must still be settleable.

A quarantined or reserved sequence blocks every sequence behind it, and the
design clears it the right way: the original request arrives again, re-derives
the same `occurrence_id`, and the row becomes a fresh attempt.
`tests/test_project_journal.py` states that in seven tests, and nothing here
changes it.

On 2026-08-30 a batch's `occurrence_id` stopped being its last event's id and
became a digest of the whole batch, because two batches ending at one event
were claiming a single name. That left rows named under the old scheme, which
no future request can produce — and on this vault three of them were holding
3 338 queued checkpoints, one being the only surviving record of 40 events.

This is the one-time repair for exactly those rows, and these tests pin what it
may and may not touch. See
`docs/research/2026-08-30-a-sequence-nothing-can-settle.md`.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import repair_orphaned_checkpoint_names as repair  # noqa: E402
from project_journal import ProjectStore  # noqa: E402
from test_project_journal import checkpoint_event  # noqa: E402


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge/projects/demo").mkdir(parents=True)
    return root


def _store(vault: Path, state_root: Path) -> ProjectStore:
    return ProjectStore(vault, state_root)


def _unsettled(store: ProjectStore) -> list[tuple[int, str]]:
    with sqlite3.connect(store.coordinator.database_path) as database:
        database.row_factory = sqlite3.Row
        rows = database.execute(
            "SELECT sequence, state FROM project_checkpoints "
            "WHERE project = 'demo' ORDER BY sequence"
        ).fetchall()
    return [(row["sequence"], row["state"]) for row in rows]


def _crashed_head(store: ProjectStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave sequence 1 quarantined, exactly as a failed apply does."""

    def crash_before_apply(transaction_id: str):
        raise RuntimeError(f"crash before {transaction_id}")

    monkeypatch.setattr(store.coordinator, "apply", crash_before_apply)
    with pytest.raises(RuntimeError, match="crash before"):
        store.checkpoint("demo", checkpoint_event("evt-first", "orphan:first"), "agent-a")
    monkeypatch.undo()
    with sqlite3.connect(store.coordinator.database_path) as database:
        database.execute(
            "UPDATE project_leases SET expires_at = '2000-01-01T00:00:00Z' "
            "WHERE project = 'demo'"
        )
        database.commit()


def test_an_orphaned_row_is_named_by_the_listing(vault: Path, state_root: Path, monkeypatch):
    store = _store(vault, state_root)
    _crashed_head(store, monkeypatch)

    rows = repair.orphaned_rows(_store(vault, state_root))

    assert [(project, sequence) for project, sequence, _s, _o in rows] == [("demo", 1)]


def test_a_batch_named_row_is_never_touched(vault: Path, state_root: Path, monkeypatch):
    """Rows the design can still clear by itself are none of this script's business."""
    store = _store(vault, state_root)
    _crashed_head(store, monkeypatch)
    with sqlite3.connect(store.coordinator.database_path) as database:
        database.execute(
            "UPDATE project_checkpoints SET occurrence_id = 'batch:abc' "
            "WHERE project = 'demo' AND sequence = 1"
        )
        database.commit()

    assert repair.orphaned_rows(_store(vault, state_root)) == []


def test_a_committed_row_is_never_touched(vault: Path, state_root: Path):
    store = _store(vault, state_root)
    store.checkpoint("demo", checkpoint_event("evt-done", "orphan:done"), "agent-a")

    assert repair.orphaned_rows(_store(vault, state_root)) == []


def test_the_repair_writes_the_orphaned_checkpoint(vault: Path, state_root: Path, monkeypatch):
    """The point of it: the event that was never written gets written."""
    store = _store(vault, state_root)
    _crashed_head(store, monkeypatch)

    lines = repair.repair(_store(vault, state_root))

    assert len(lines) == 1
    assert "written" in lines[0]
    assert _unsettled(_store(vault, state_root)) == [(1, "committed")]


def test_the_written_event_is_the_one_that_was_owed(vault: Path, state_root: Path, monkeypatch):
    store = _store(vault, state_root)
    _crashed_head(store, monkeypatch)

    repair.repair(_store(vault, state_root))

    journal = _store(vault, state_root).read_journal("demo")
    events = [json.loads(line) for line in journal.splitlines() if line.startswith("{")]
    assert [event["occurrence_id"] for event in events] == ["evt-first"]


def test_a_sequence_behind_the_orphan_appends_after_it(
    vault: Path, state_root: Path, monkeypatch
):
    """Head-of-line blocking is what this exists to end; order must survive it."""
    store = _store(vault, state_root)
    _crashed_head(store, monkeypatch)
    repair.repair(_store(vault, state_root))

    _store(vault, state_root).checkpoint(
        "demo", checkpoint_event("evt-second", "orphan:second"), "agent-b"
    )

    journal = _store(vault, state_root).read_journal("demo")
    events = [json.loads(line) for line in journal.splitlines() if line.startswith("{")]
    assert [event["sequence"] for event in events] == [1, 2]
    assert [event["occurrence_id"] for event in events] == ["evt-first", "evt-second"]


def test_running_it_twice_changes_nothing(vault: Path, state_root: Path, monkeypatch):
    store = _store(vault, state_root)
    _crashed_head(store, monkeypatch)
    repair.repair(_store(vault, state_root))

    assert repair.repair(_store(vault, state_root)) == []


def test_a_clean_vault_is_left_alone(vault: Path, state_root: Path):
    assert repair.repair(_store(vault, state_root)) == []


def test_the_retry_reports_nothing_when_the_row_already_settled(
    vault: Path, state_root: Path, monkeypatch
):
    """A row settled between the listing and the lease is not an error."""
    store = _store(vault, state_root)
    _crashed_head(store, monkeypatch)
    repair.repair(_store(vault, state_root))

    assert repair.repair_one(_store(vault, state_root), "demo", 1) == "already settled"


def test_a_batch_name_that_will_never_return_is_repaired_once_it_is_old(monkeypatch) -> None:
    """A batch name was assumed re-requestable. On 2026-09-05 that failed.

    A batch checkpoint hit `precondition_failed` — the journal had moved between
    planning and applying — and its `occurrence_id` is a digest of exactly the
    events in that batch, which were consumed and will never be assembled again.
    One project sat quarantined with 335 events queued behind it, and every
    later attempt logged `ProjectPendingPriorError` instead of making progress.

    Age is the only readable signal: a request that was going to return has
    returned within the window.
    """
    from datetime import datetime, timedelta, timezone

    import repair_orphaned_checkpoint_names as repair

    now = datetime.now(timezone.utc)
    old = (now - timedelta(seconds=repair.STALE_CHECKPOINT_SECONDS + 60)).isoformat()
    fresh = (now - timedelta(seconds=60)).isoformat()

    assert repair._is_orphaned(
        {"occurrence_id": "batch:abc", "created_at": old}, now.timestamp()
    )
    assert not repair._is_orphaned(
        {"occurrence_id": "batch:abc", "created_at": fresh}, now.timestamp()
    )


def test_a_name_from_before_the_batch_scheme_is_repaired_at_any_age() -> None:
    from datetime import datetime, timezone

    import repair_orphaned_checkpoint_names as repair

    now = datetime.now(timezone.utc)

    assert repair._is_orphaned(
        {"occurrence_id": "event-1", "created_at": now.isoformat()}, now.timestamp()
    )


def test_a_row_with_no_transaction_time_is_treated_as_walled_up() -> None:
    """No timestamp is not evidence that a request is still coming."""
    import repair_orphaned_checkpoint_names as repair

    assert repair._is_orphaned({"occurrence_id": "batch:abc", "created_at": None}, 0.0)
