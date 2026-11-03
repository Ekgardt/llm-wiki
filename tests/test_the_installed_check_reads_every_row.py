"""The installed-vault check reads every operational row, not the first 10 000.

`installed_memory_repair._bounded_rows` raised past 10 000 rows and the caller
reported `transaction_state_unreadable`, which refuses a backup; the unpruned rows
are the two-day undo window, whose size is activity. See
`docs/research/2026-09-27-doctor-reads-every-transaction.md`.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import installed_memory_repair

from tests.slow_machine import LONG_TIMEOUT
from tests.test_reliability_v3_adoption import _vault, build_adopted_reliability_v3

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
PAST_THE_OLD_CAP = 10_050
_INSERT = (
    'INSERT INTO "transaction"(id, operation_id, request_hash, state, preconditions_json,'
    " plan_hash, created_at, updated_at) VALUES (?, ?, ?, ?, '{}', ?, ?, ?)"
)


def _row(index: int, state: str, when: datetime) -> tuple[str, ...]:
    stamp = when.isoformat().replace("+00:00", "Z")
    return (f"t{index:06d}", f"op-{index}", "0" * 64, state, "1" * 64, stamp, stamp)


def _adopted_with_rows(tmp_path: Path, rows: list[tuple[str, ...]]) -> Path:
    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    with sqlite3.connect(state_root / "run" / "markdown-transactions-v3.sqlite3") as database:
        database.executemany(_INSERT, rows)
    return state_root


def _old_committed(count: int) -> list[tuple[str, ...]]:
    old = NOW - timedelta(days=30)
    return [_row(index, "committed", old) for index in range(count)]


def _coordinator_blockers(state_root: Path) -> list[str]:
    return installed_memory_repair.validate_coordinator_v3_runtime(
        state_root=state_root, now=NOW, deadline=time.monotonic() + LONG_TIMEOUT, excluded_owner=None
    )


def test_a_window_past_the_old_cap_is_read_not_refused(tmp_path: Path) -> None:
    state_root = _adopted_with_rows(tmp_path, _old_committed(PAST_THE_OLD_CAP))

    assert "transaction_state_unreadable" not in _coordinator_blockers(state_root)


def test_a_quarantine_after_the_first_ten_thousand_rows_is_found(tmp_path: Path) -> None:
    rows = [*_old_committed(PAST_THE_OLD_CAP), _row(PAST_THE_OLD_CAP, "quarantined", NOW)]
    state_root = _adopted_with_rows(tmp_path, rows)

    blockers = _coordinator_blockers(state_root)

    assert "transaction_state_unreadable" not in blockers
    assert "transaction_quarantined" in blockers
