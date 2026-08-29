"""A bounded read may refuse deletion; it may not allege corruption.

The doctor caps every operational scan at ``MAX_OPERATIONAL_ROWS``. The
``operation`` table is larger than the ``transaction`` table by construction --
every transaction that is neither preparing nor discarded must own at least one
operation -- so the operation scan is the first to hit the ceiling, and it hits
it during ordinary growth rather than under attack.

Two false statements followed from that. The truncation appended
``transaction_state_unknown``, a corruption claim, to the deletion codes. And
the transactions whose operations fell past the ceiling looked like
transactions with no operations at all, which the row check reads as corrupt.

Both are permanent, because the table only grows, so both contradict
``knowledge/notes/self-resolving-health-findings-decision.md``.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import doctor  # noqa: E402

_SCHEMA = """
CREATE TABLE "transaction" (
    id TEXT PRIMARY KEY, operation_id TEXT, request_hash TEXT,
    state TEXT, preconditions_json TEXT, plan_hash TEXT,
    created_at TEXT, updated_at TEXT, artifacts_pruned_at TEXT
);
CREATE TABLE "operation" (
    transaction_id TEXT, position INTEGER, kind TEXT, path TEXT,
    before_hash TEXT, after_hash TEXT, parent_device INTEGER,
    parent_inode INTEGER, applied INTEGER
);
"""


def _transaction_values(
    transactions: int, state: str, plan_hash: str, stamp: str
) -> list[tuple]:
    return [
        (
            f"tx-{index:06d}",
            f"operation-{index}",
            "a" * 64,
            state,
            "{}",
            plan_hash,
            stamp,
            stamp,
        )
        for index in range(transactions)
    ]


def _operation_values(transactions: int, operations_each: int) -> list[tuple]:
    return [
        (
            f"tx-{index:06d}",
            position,
            "create",
            f"knowledge/notes/page-{index}-{position}.md",
            "absent",
            "c" * 64,
        )
        for index in range(transactions)
        for position in range(operations_each)
    ]


def _write_undo_artifacts(state_root: Path, transactions: int, state: str) -> None:
    """Only a live transaction keeps its artifact; discard removes it."""
    if state == "discarded":
        return
    for index in range(transactions):
        (state_root / "run/transactions" / f"tx-{index:06d}").mkdir(parents=True)


def _build_vault(
    state_root: Path,
    now: datetime,
    *,
    transactions: int,
    operations_each: int,
    state: str = "committed",
    plan_hash: str = "b" * 64,
) -> None:
    """A vault of healthy transactions, each owning contiguous work."""
    database = state_root / "run/markdown-transactions.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    stamp = now.isoformat()
    with sqlite3.connect(database) as connection:
        connection.executescript(_SCHEMA)
        connection.executemany(
            'INSERT INTO "transaction" VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)',
            _transaction_values(transactions, state, plan_hash, stamp),
        )
        connection.executemany(
            'INSERT INTO "operation" VALUES (?, ?, ?, ?, ?, ?, 1, 2, 1)',
            _operation_values(transactions, operations_each),
        )
    _write_undo_artifacts(state_root, transactions, state)


def _check(state_root: Path, now: datetime) -> dict:
    return doctor._transaction_check(state_root, now, deadline=float("inf"))


@pytest.fixture(name="now")
def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_an_operation_table_over_the_ceiling_is_not_called_corrupt(
    tmp_path: Path, now: datetime
) -> None:
    """The rows the scan never read cannot be evidence of anything."""
    per_transaction = 3
    count = doctor.MAX_OPERATIONAL_ROWS // per_transaction + 200
    _build_vault(
        tmp_path, now, transactions=count, operations_each=per_transaction
    )

    details = _check(tmp_path, now)["details"]

    assert "transaction_operation_scan_truncated" in details["codes"]
    assert "transaction_metadata_corrupt" not in details["codes"]
    assert "transaction_state_corrupt" not in details["deletion_codes"]


def test_a_truncated_scan_does_not_claim_an_unknown_transaction_state(
    tmp_path: Path, now: datetime
) -> None:
    """`transaction_state_unknown` is a claim about a row that was read."""
    per_transaction = 3
    count = doctor.MAX_OPERATIONAL_ROWS // per_transaction + 200
    _build_vault(
        tmp_path, now, transactions=count, operations_each=per_transaction
    )

    details = _check(tmp_path, now)["details"]

    assert "transaction_state_unknown" not in details["deletion_codes"]


def test_a_truncated_scan_still_refuses_deletion(
    tmp_path: Path, now: datetime
) -> None:
    """Fail closed: a read that could not see every row cannot permit deletion."""
    per_transaction = 3
    count = doctor.MAX_OPERATIONAL_ROWS // per_transaction + 200
    _build_vault(
        tmp_path, now, transactions=count, operations_each=per_transaction
    )

    details = _check(tmp_path, now)["details"]

    assert "transaction_scan_incomplete" in details["deletion_codes"]


def test_a_healthy_vault_over_the_ceiling_is_not_in_error(
    tmp_path: Path, now: datetime
) -> None:
    """Ordinary growth past a read ceiling is not a health problem."""
    per_transaction = 3
    count = doctor.MAX_OPERATIONAL_ROWS // per_transaction + 200
    _build_vault(
        tmp_path, now, transactions=count, operations_each=per_transaction
    )

    assert _check(tmp_path, now)["status"] == "ok"


def test_a_vault_under_the_ceiling_is_unaffected(
    tmp_path: Path, now: datetime
) -> None:
    """The change touches only the truncated path."""
    _build_vault(tmp_path, now, transactions=50, operations_each=3)

    result = _check(tmp_path, now)

    assert result["status"] == "ok"
    assert result["details"]["codes"] == []
    assert "transaction_scan_incomplete" not in result["details"]["deletion_codes"]
    assert result["details"]["states"]["committed"] == 50


def test_a_real_unknown_state_is_still_an_error(
    tmp_path: Path, now: datetime
) -> None:
    """A state string outside the known set is corruption, read or not."""
    _build_vault(tmp_path, now, transactions=5, operations_each=1, state="invented")

    result = _check(tmp_path, now)

    assert result["status"] == "error"
    assert "transaction_state_unknown" in result["details"]["deletion_codes"]


def test_a_missing_operation_under_the_ceiling_is_still_corrupt(
    tmp_path: Path, now: datetime
) -> None:
    """A complete read that finds no operations still accuses."""
    _build_vault(tmp_path, now, transactions=5, operations_each=1)
    database = tmp_path / "run/markdown-transactions.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            'DELETE FROM "operation" WHERE transaction_id = ?', ("tx-000003",)
        )

    result = _check(tmp_path, now)

    assert result["status"] == "error"
    assert "transaction_state_corrupt" in result["details"]["deletion_codes"]


def test_a_transaction_discarded_before_planning_is_not_corrupt(
    tmp_path: Path, now: datetime
) -> None:
    """`_promoted_for_recovery` discards straight out of `preparing`.

    That path never reaches `prepared`, so `plan_hash` is still the empty
    string the insert wrote. Only `preparing` was exempt, so every such row
    was permanently reported as corrupt metadata.
    """
    _build_vault(
        tmp_path,
        now,
        transactions=3,
        operations_each=0,
        state="discarded",
        plan_hash="",
    )

    result = _check(tmp_path, now)

    assert result["status"] == "ok"
    assert "transaction_metadata_corrupt" not in result["details"]["codes"]


def test_a_committed_transaction_still_needs_a_plan_hash(
    tmp_path: Path, now: datetime
) -> None:
    """The exemption is for the state that legitimately never planned."""
    _build_vault(
        tmp_path, now, transactions=3, operations_each=1, plan_hash=""
    )

    result = _check(tmp_path, now)

    assert result["status"] == "error"
    assert "transaction_state_corrupt" in result["details"]["deletion_codes"]
