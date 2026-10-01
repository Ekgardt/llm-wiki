"""A health read judges every transaction row, and only on evidence.

The doctor used to cap the transaction and operation scans at
``MAX_OPERATIONAL_ROWS`` (10 000). The installed vault outgrew it (29 275
transactions, 37 509 operations on 2026-09-27), so every report judged a third
of the rows and said so. Both tables are now streamed whole; these tests hold a
vault past the old cap to the same truth as a small one: no truncation, no
corruption alleged from rows unread, and a real defect found wherever it lies.
See ``docs/research/2026-09-27-doctor-reads-every-transaction.md``.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import doctor  # noqa: E402

# The row cap the transaction scans used to have; a vault past it is ordinary.
OLD_SCAN_CAP = 10_000

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
    """Every operation is read, so a healthy table past the old cap is healthy."""
    per_transaction = 3
    count = OLD_SCAN_CAP // per_transaction + 200
    _build_vault(
        tmp_path, now, transactions=count, operations_each=per_transaction
    )

    details = _check(tmp_path, now)["details"]

    assert details["codes"] == []
    assert "transaction_state_corrupt" not in details["deletion_codes"]


def test_a_scan_past_the_old_cap_claims_no_unknown_transaction_state(
    tmp_path: Path, now: datetime
) -> None:
    """`transaction_state_unknown` is a claim about a row that was read."""
    per_transaction = 3
    count = OLD_SCAN_CAP // per_transaction + 200
    _build_vault(
        tmp_path, now, transactions=count, operations_each=per_transaction
    )

    details = _check(tmp_path, now)["details"]

    assert "transaction_state_unknown" not in details["deletion_codes"]


def test_a_table_past_the_old_cap_is_read_whole(
    tmp_path: Path, now: datetime
) -> None:
    """No row cap, so no incomplete read: every row is counted by its state."""
    per_transaction = 3
    count = OLD_SCAN_CAP // per_transaction + 200
    _build_vault(
        tmp_path, now, transactions=count, operations_each=per_transaction
    )

    details = _check(tmp_path, now)["details"]

    assert "transaction_scan_incomplete" not in details["deletion_codes"]
    assert details["states"]["committed"] == count


def test_a_healthy_vault_over_the_ceiling_is_not_in_error(
    tmp_path: Path, now: datetime
) -> None:
    """Ordinary growth past the old cap is not a health problem."""
    per_transaction = 3
    count = OLD_SCAN_CAP // per_transaction + 200
    _build_vault(
        tmp_path, now, transactions=count, operations_each=per_transaction
    )

    assert _check(tmp_path, now)["status"] == "ok"


def test_a_vault_under_the_ceiling_is_unaffected(
    tmp_path: Path, now: datetime
) -> None:
    """A small vault reads as it always did."""
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


def test_a_missing_operation_is_still_corrupt(
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


def _set_plan_hash(state_root: Path, transaction_id: str, plan_hash: str) -> None:
    database = state_root / "run/markdown-transactions.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            'UPDATE "transaction" SET plan_hash = ? WHERE id = ?', (plan_hash, transaction_id)
        )


def _delete_operations(state_root: Path, transaction_id: str) -> None:
    database = state_root / "run/markdown-transactions.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute('DELETE FROM "operation" WHERE transaction_id = ?', (transaction_id,))


def _corrupt_codes(state_root: Path, now: datetime) -> tuple[bool, bool]:
    details = _check(state_root, now)["details"]
    return (
        "transaction_metadata_corrupt" in details["codes"],
        "transaction_state_corrupt" in details["deletion_codes"],
    )


def test_a_corrupt_row_past_the_old_cap_is_found(tmp_path: Path, now: datetime) -> None:
    """Past the cap the verdict was about the cap, not the rows: the capped scan called
    this healthy vault corrupt, and the oldest rows it left unread could hide a real defect."""
    _build_vault(tmp_path, now, transactions=OLD_SCAN_CAP + 200, operations_each=1)
    healthy = _corrupt_codes(tmp_path, now)
    _set_plan_hash(tmp_path, "tx-000000", "")

    assert (healthy, _corrupt_codes(tmp_path, now)) == ((False, False), (True, True))


def test_a_missing_operation_past_the_old_cap_is_found(tmp_path: Path, now: datetime) -> None:
    """A truncated operation read abstained on every row; a whole read accuses on evidence."""
    _build_vault(tmp_path, now, transactions=OLD_SCAN_CAP // 3 + 200, operations_each=3)
    _delete_operations(tmp_path, "tx-000000")

    assert _corrupt_codes(tmp_path, now) == (True, True)


def test_an_operation_without_its_transaction_is_corrupt(tmp_path: Path, now: datetime) -> None:
    """The operation read joined its transaction, so an orphan operation vanished from view."""
    _build_vault(tmp_path, now, transactions=3, operations_each=1)
    database = tmp_path / "run/markdown-transactions.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            'INSERT INTO "operation" VALUES (?, 0, ?, ?, ?, ?, 1, 2, 1)',
            ("tx-gone", "create", "knowledge/notes/orphan.md", "absent", "c" * 64),
        )

    assert _corrupt_codes(tmp_path, now) == (True, True)


def test_a_transaction_quarantined_before_planning_is_not_corrupt(
    tmp_path: Path, now: datetime
) -> None:
    """`_commit_promotion` rolls back the plan and operations, then quarantines the row."""
    _build_vault(
        tmp_path, now, transactions=3, operations_each=0, state="quarantined", plan_hash=""
    )

    assert _corrupt_codes(tmp_path, now) == (False, False)


def test_a_quarantined_transaction_with_a_plan_still_owns_operations(
    tmp_path: Path, now: datetime
) -> None:
    """The exemption is for a row that never planned, not for a planned one missing work."""
    _build_vault(tmp_path, now, transactions=3, operations_each=0, state="quarantined")

    assert _corrupt_codes(tmp_path, now) == (True, True)


def test_an_undo_listing_past_old_bound_is_fully_reconciled(
    tmp_path: Path, now: datetime
) -> None:
    """Every retained directory is compared with the ledger, beyond the old cap."""
    _build_vault(tmp_path, now, transactions=OLD_SCAN_CAP + 50, operations_each=1)

    details = _check(tmp_path, now)["details"]

    assert (
        "transaction_artifact_state_unknown" in details["deletion_codes"],
        "transaction_metadata_corrupt" in details["codes"],
    ) == (False, False)


def test_filesystem_scan_does_not_hold_the_database_read_lock(tmp_path, now, monkeypatch):
    """A writer can commit while doctor is examining undo files, without sleeps."""
    _build_vault(tmp_path, now, transactions=3, operations_each=1)
    original = doctor._checked_artifacts
    committed = []

    def inspect_after_writer(*args):
        path = tmp_path / "run/markdown-transactions.sqlite3"
        with sqlite3.connect(path, timeout=0) as writer:
            writer.execute('UPDATE "transaction" SET operation_id=operation_id WHERE id=?', ("tx-000000",))
        committed.append(True)
        return original(*args)

    monkeypatch.setattr(doctor, "_checked_artifacts", inspect_after_writer)
    result = _check(tmp_path, now)
    assert committed == [True], result
    assert result["status"] == "ok", result


def test_rows_remain_coherent_when_a_writer_commits_during_file_checks(tmp_path, now, monkeypatch):
    _build_vault(tmp_path, now, transactions=3, operations_each=1)
    original = doctor._checked_artifacts

    def corrupt_after_snapshot(*args):
        _delete_operations(tmp_path, "tx-000000")
        return original(*args)

    with monkeypatch.context() as context:
        context.setattr(doctor, "_checked_artifacts", corrupt_after_snapshot)
        snapshot = _check(tmp_path, now)
    assert snapshot["status"] == "ok", snapshot
    assert "transaction_state_corrupt" in _check(tmp_path, now)["details"]["deletion_codes"]


def test_copied_rows_still_obey_the_doctor_deadline():
    with pytest.raises(TimeoutError, match="deadline"):
        list(doctor._checked_snapshot_rows([None], 0))


def test_queue_artifacts_past_old_count_cap_are_counted(tmp_path):
    import time

    directory = tmp_path / "run/queue-results"
    directory.mkdir(parents=True)
    for number in range(OLD_SCAN_CAP + 1):
        (directory / f"{number}.json").write_text("{}")
    state = doctor._queue_artifact_state(tmp_path, time.monotonic() + 30)
    assert state["results_retained"] == OLD_SCAN_CAP + 1
    assert state["artifact_truncated"] is False
    assert "queue_result_retained" in state["deletion_codes"]


def test_installed_runtime_lists_past_old_count_cap(tmp_path):
    import time

    import installed_memory_repair as repair

    directory = tmp_path / "run/transactions"
    directory.mkdir(parents=True)
    for number in range(OLD_SCAN_CAP + 1):
        (directory / f"{number:032x}").mkdir()
    entries = repair._bounded_entries(directory, state_root=tmp_path, deadline=time.monotonic() + 30)
    assert len(entries) == OLD_SCAN_CAP + 1


@pytest.mark.parametrize("inspector", ["doctor", "installed"])
def test_runtime_directory_deadline_still_refuses_incomplete_scan(tmp_path, inspector):
    import time

    import installed_memory_repair as repair

    directory = tmp_path / "run/queue-results"
    directory.mkdir(parents=True)
    (directory / "retained.json").write_text("{}")
    expired = time.monotonic() - 1
    if inspector == "installed":
        with pytest.raises(TimeoutError):
            repair._bounded_entries(directory, state_root=tmp_path, deadline=expired)
        return
    state = doctor._queue_artifact_state(tmp_path, expired)
    assert state["artifact_truncated"] is True
    assert "queue_artifact_state_unknown" in state["deletion_codes"]
