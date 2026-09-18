"""The queue-ownership registry closes each handle where it opens it.

`sqlite3.Connection`'s context manager settles the transaction and leaves the
handle open, so these four callers left the close to deallocation. Wrapping them
in `closing` states it instead — and must not cost the transaction the handle was
opened for, which is what these tests hold.

They do not assert the absence of a leak: on CPython the descriptor goes with the
last reference, so there is no observable difference to assert. See
`docs/research/2026-09-18-a-connection-is-closed-by-whoever-opened-it.md`.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import memory_queue  # noqa: E402

_NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)


def _owner_row(state_root: Path) -> tuple | None:
    with sqlite3.connect(state_root / "run" / "queue.sqlite3") as connection:
        return connection.execute(
            "SELECT token, pid FROM queue_ownership WHERE role='migration'"
        ).fetchone()


def test_taking_renewing_and_releasing_an_owner_still_writes_its_row(
    tmp_path: Path,
) -> None:
    owner = memory_queue._acquire_queue_owner(
        tmp_path, "migration", "migration_busy", now=_NOW, ttl_seconds=60
    )
    held = _owner_row(tmp_path)

    renewed = memory_queue._heartbeat_queue_owner(owner)
    released = memory_queue._release_queue_owner(renewed)

    assert (held, released, _owner_row(tmp_path)) == (
        (owner.token, owner.pid),
        True,
        (None, None),
    )


def test_a_second_owner_is_still_refused_while_the_first_holds(tmp_path: Path) -> None:
    owner = memory_queue._acquire_queue_owner(
        tmp_path, "migration", "migration_busy", now=_NOW, ttl_seconds=60
    )

    with pytest.raises(memory_queue.QueueOperationError) as raised:
        memory_queue._acquire_queue_owner(
            tmp_path, "migration", "migration_busy", now=_NOW, ttl_seconds=60
        )

    assert (raised.value.code, memory_queue._release_queue_owner(owner)) == (
        "migration_busy",
        True,
    )
