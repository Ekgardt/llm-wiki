"""A sequence the database calls committed must be in the journal.

`ProjectJournalRebuildRequired` says an append-only journal ordering fault needs
an explicit verified rebuild. Nothing performed one, so a sequence marked
committed without its entry stopped that project's journal for good.

The state is not supposed to arise. On this vault it arose on 2026-08-30 from a
change of mine that lived for about an hour: a rule that settled a reservation
whose evidence looked already journaled marked `llm-wiki` 1429 committed, the
drain removed its 28 events from the queue, and no entry was written. Nothing
was lost — those 28 are a strict suffix of the 68 in the journaled 1428 — but
1430 then refused to follow.

These tests pin what the repair may and may not do. The journal is the
authority: a committed row it does not carry is a false record whose write is
still owed. Everything else is left alone.

See `docs/research/2026-08-30-a-sequence-nothing-can-settle.md`.
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

import repair_journal_gap as repair  # noqa: E402
from project_journal import JOURNAL_HEADER, ProjectStore  # noqa: E402
from test_project_journal import checkpoint_event  # noqa: E402


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge/projects/demo").mkdir(parents=True)
    return root


def _store(vault: Path, state_root: Path) -> ProjectStore:
    return ProjectStore(vault, state_root)


def _write_two(store: ProjectStore) -> None:
    store.checkpoint("demo", checkpoint_event("evt-1", "gap:1"), "agent-a")
    store.checkpoint("demo", checkpoint_event("evt-2", "gap:2"), "agent-a")


def _drop_last_entry(vault: Path) -> None:
    """Leave sequence 2 committed in the database and absent from the journal."""
    path = vault / "knowledge/projects/demo/journal.md"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")


def _entries(vault: Path) -> list[dict]:
    text = (vault / "knowledge/projects/demo/journal.md").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


def test_a_missing_committed_sequence_is_found(vault: Path, state_root: Path):
    store = _store(vault, state_root)
    _write_two(store)
    _drop_last_entry(vault)

    assert repair.missing_sequences(_store(vault, state_root), "demo") == [2]


def test_a_whole_journal_is_reported_clean(vault: Path, state_root: Path):
    store = _store(vault, state_root)
    _write_two(store)

    assert repair.missing_sequences(_store(vault, state_root), "demo") == []


def test_the_repair_appends_the_missing_entry(vault: Path, state_root: Path):
    store = _store(vault, state_root)
    _write_two(store)
    _drop_last_entry(vault)

    lines = repair.repair(_store(vault, state_root), "demo")

    assert lines == ["demo 2: appended"]
    assert [entry["sequence"] for entry in _entries(vault)] == [1, 2]


def test_the_appended_entry_is_the_one_the_database_holds(vault: Path, state_root: Path):
    store = _store(vault, state_root)
    _write_two(store)
    _drop_last_entry(vault)

    repair.repair(_store(vault, state_root), "demo")

    assert [entry["occurrence_id"] for entry in _entries(vault)] == ["evt-1", "evt-2"]


def test_the_journal_can_be_written_again_afterwards(vault: Path, state_root: Path):
    """The point of it: the project stops being stopped."""
    store = _store(vault, state_root)
    _write_two(store)
    _drop_last_entry(vault)
    repair.repair(_store(vault, state_root), "demo")

    _store(vault, state_root).checkpoint(
        "demo", checkpoint_event("evt-3", "gap:3"), "agent-b"
    )

    assert [entry["sequence"] for entry in _entries(vault)] == [1, 2, 3]


def test_running_it_twice_appends_nothing(vault: Path, state_root: Path):
    store = _store(vault, state_root)
    _write_two(store)
    _drop_last_entry(vault)
    repair.repair(_store(vault, state_root), "demo")

    assert repair.repair(_store(vault, state_root), "demo") == []


def test_a_clean_journal_is_left_alone(vault: Path, state_root: Path):
    store = _store(vault, state_root)
    _write_two(store)

    assert repair.repair(_store(vault, state_root), "demo") == []
    assert [entry["sequence"] for entry in _entries(vault)] == [1, 2]


def test_a_gap_that_is_not_at_the_head_stops_the_walk(vault: Path, state_root: Path):
    """Never skip a sequence: the walk ends at the first one it cannot place."""
    store = _store(vault, state_root)
    _write_two(store)
    path = vault / "knowledge/projects/demo/journal.md"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(JOURNAL_HEADER, encoding="utf-8")
    with sqlite3.connect(store.coordinator.database_path) as database:
        database.execute(
            "UPDATE project_checkpoints SET state = 'reserved' "
            "WHERE project = 'demo' AND sequence = 1"
        )
        database.commit()
    assert lines  # the journal did hold both entries before it was emptied

    assert repair.missing_sequences(_store(vault, state_root), "demo") == []


def test_an_empty_journal_is_filled_from_the_head(vault: Path, state_root: Path):
    store = _store(vault, state_root)
    _write_two(store)
    (vault / "knowledge/projects/demo/journal.md").write_text(
        JOURNAL_HEADER, encoding="utf-8"
    )

    assert repair.missing_sequences(_store(vault, state_root), "demo") == [1, 2]
