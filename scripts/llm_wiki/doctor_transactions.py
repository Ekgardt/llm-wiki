"""Doctor's inspection of the Markdown transaction tables and quarantined compile history."""

from __future__ import annotations

import contextlib
import hashlib
import json
import lzma
import math
import os
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

import generation_maintenance  # noqa: E402
import reliable_memory
from bounded_io import open_stable_range, read_stable_bytes
from llm_wiki import doctor_base, monotonic_deadline
from llm_wiki.doctor_base import (
    MAX_OPERATIONAL_ROWS,
    TRANSACTION_STATES,
    database_sidecar_present,
    database_tables,
    owner_row_known,
)
from llm_wiki.evidence_reference import MAX_DAILY_BYTES
from markdown_transaction import (
    UNDO_RETENTION_DAYS,  # noqa: E402
    MarkdownCoordinator,
)
from reliable_memory import (
    read_runtime_bytes,
)
from transaction_lineage import (
    base_operation_identity,
    committed_created_paths,
    outcome_was_written,
    resolved_by_lineage,
)

# Transactions still on their way somewhere: a crash here needs recovery.
UNSETTLED_TRANSACTION_STATES = ("preparing", "prepared", "applying", "aborting")


TRANSACTION_REQUIRED_COLUMNS = {
    "id",
    "operation_id",
    "request_hash",
    "state",
    "preconditions_json",
    "plan_hash",
    "created_at",
    "updated_at",
    "artifacts_pruned_at",
}
OPERATION_REQUIRED_COLUMNS = {
    "transaction_id",
    "position",
    "kind",
    "path",
    "before_hash",
    "after_hash",
    "parent_device",
    "parent_inode",
    "applied",
}


def operational_database_path(state_root: Path, database_name: str) -> Path:
    """The legacy path, or the database its adoption tombstone names.

    Reliability V3 adoption replaces the legacy path with a JSON tombstone.
    Reading that as SQLite is what made a healthy adopted vault report
    `queue_state_unreadable` and `transaction_state_unreadable`.
    """
    from llm_wiki.adoption_admission import (
        adopted_database_path,
    )

    return adopted_database_path(database_name=database_name, state_root=state_root)


def transaction_artifacts(state_root: Path, deadline: float) -> tuple[set[str], bool]:
    directory = state_root / "run" / "transactions"
    kind, _ = generation_maintenance.safe_kind(directory, state_root)
    if monotonic_deadline.deadline_reached(deadline):
        return set(), True
    if kind == "missing":
        return set(), False
    return _transaction_directory_inventory(directory, state_root, kind, deadline)


def _transaction_directory_inventory(
    directory: Path, state_root: Path, kind: str, deadline: float
) -> tuple[set[str], bool]:
    if kind != "directory":
        return set(), True
    identifiers: set[str] = set()
    try:
        with os.scandir(directory) as entries:
            incomplete = _collect_transaction_identifiers(entries, state_root, deadline, identifiers)
    except OSError:
        return identifiers, True
    return identifiers, incomplete


def _collect_transaction_identifiers(
    entries: Iterator[os.DirEntry], state_root: Path, deadline: float, identifiers: set[str]
) -> bool:
    incomplete = False
    for entry in entries:
        if monotonic_deadline.deadline_reached(deadline):
            return True
        kind = artifact_kind(Path(entry.path), state_root)
        incomplete = incomplete or kind == "unsafe"
        _retain_transaction_identifier(identifiers, entry.name, kind)
    return incomplete or monotonic_deadline.deadline_reached(deadline)


def _retain_transaction_identifier(identifiers: set[str], name: str, kind: str) -> None:
    if kind == "artifact":
        identifiers.add(name)


def artifact_kind(entry: Path, state_root: Path) -> str:
    """`artifact`, `staged` (images a prune set aside and the next prune or repair removes), or `unsafe`.

    A staged prune read as an unsafe entry, so an interrupted prune looked like a
    damaged transaction trail (audit 2026-09-26 B-22).
    """
    if generation_maintenance.safe_kind(entry, state_root)[0] != "directory":
        return "unsafe"
    if entry.name.startswith(".") and ".pruning-" in entry.name:
        return "staged"
    return "artifact" if _TRANSACTION_ID_RE.fullmatch(entry.name) else "unsafe"


_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_TRANSACTION_ID_RE = re.compile(r"[0-9a-z_-]{1,128}")


# Complete SQL rows are copied in one short consistent snapshot. Cursors and
# the read connection close before filesystem inspection; all significant rows
# are compared exactly in a final streaming SQL snapshot before the verdict.
# The operation query has no join: an operation whose transaction is absent
# reaches the identity check instead of disappearing from the inspection.
_OPERATION_QUERY = (
    "SELECT transaction_id, position, kind, path, before_hash, after_hash, "
    'parent_device, parent_inode, applied FROM "operation"'
)
_OWNER_TABLE_QUERIES = {
    "writer_owners": "SELECT * FROM writer_owners LIMIT ?",
    "maintenance_owners": "SELECT * FROM maintenance_owners LIMIT ?",
}


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST_RE.fullmatch(value) is not None


def _valid_operation_hash(value: object) -> bool:
    """An operation names either a digest or the absence of content."""
    return value == "absent" or _is_digest(value)


# Which side of an operation is allowed to be absent, per kind.
_OPERATION_ABSENCE = {
    "create": (True, False),
    "replace": (False, False),
    "delete": (False, True),
}


def _valid_operation_transition(kind: object, before: object, after: object) -> bool:
    expected = _OPERATION_ABSENCE.get(kind)
    if expected is None:
        return False
    return (before == "absent", after == "absent") == expected


def _valid_operation_position(position: object) -> bool:
    if isinstance(position, bool) or not isinstance(position, int):
        return False
    return position >= 0


def _valid_operation_identity(operation: sqlite3.Row, known_ids: set[str]) -> bool:
    transaction_id = operation["transaction_id"]
    if not isinstance(transaction_id, str) or transaction_id not in known_ids:
        return False
    if not _valid_operation_position(operation["position"]):
        return False
    return isinstance(operation["path"], str) and bool(operation["path"])


def _valid_operation_change(operation: sqlite3.Row) -> bool:
    before = operation["before_hash"]
    after = operation["after_hash"]
    if not _valid_operation_hash(before) or not _valid_operation_hash(after):
        return False
    return _valid_operation_transition(operation["kind"], before, after)


def _valid_operation_parent(operation: sqlite3.Row) -> bool:
    return (
        isinstance(operation["parent_device"], int)
        and isinstance(operation["parent_inode"], int)
        and operation["applied"] in {0, 1}
    )


def _valid_operation_row(operation: sqlite3.Row, known_ids: set[str]) -> bool:
    if not _valid_operation_identity(operation, known_ids):
        return False
    if not _valid_operation_change(operation):
        return False
    return _valid_operation_parent(operation)


def _operation_positions(
    operation_rows: Iterator[sqlite3.Row], known_ids: set[str]
) -> tuple[dict[str, list[int]], bool]:
    """Positions recorded per transaction, and whether any row was malformed."""
    positions: dict[str, list[int]] = {
        transaction_id: [] for transaction_id in known_ids
    }
    corrupt = False
    for operation in operation_rows:
        if not _valid_operation_row(operation, known_ids):
            corrupt = True
            continue
        positions[operation["transaction_id"]].append(operation["position"])
    return positions, corrupt


# States a row can reach straight out of `preparing`, before any plan or operation was
# written: `_promoted_for_recovery` discards, and `_commit_promotion` quarantines when
# binding the project reservation refuses, rolling back the plan and operations it was
# writing in the same database transaction (9 such rows on the installed vault,
# 2026-09-27; docs/research/2026-09-27-doctor-reads-every-transaction.md).
_UNPLANNED_STATES = frozenset({"preparing", "discarded", "quarantined"})


def _never_planned(row: sqlite3.Row, state: str) -> bool:
    return row["plan_hash"] == "" and state in _UNPLANNED_STATES


def _valid_plan_hash(row: sqlite3.Row, state: str) -> bool:
    """A plan hash is absent exactly while no plan was ever computed."""
    return _never_planned(row, state) or _is_digest(row["plan_hash"])


def _loaded_preconditions(row: sqlite3.Row) -> object:
    try:
        return json.loads(row["preconditions_json"])
    except (TypeError, ValueError):
        return None


def _valid_transaction_identity(row: sqlite3.Row) -> bool:
    transaction_id = row["id"]
    if not isinstance(transaction_id, str) or not _TRANSACTION_ID_RE.fullmatch(
        transaction_id
    ):
        return False
    if not isinstance(row["operation_id"], str) or not row["operation_id"]:
        return False
    return _is_digest(row["request_hash"])


def _valid_transaction_payload(row: sqlite3.Row, state: str) -> bool:
    if not _valid_plan_hash(row, state):
        return False
    return isinstance(_loaded_preconditions(row), dict)


def _valid_transaction_timestamps(row: sqlite3.Row) -> bool:
    created = generation_maintenance.parse_utc(row["created_at"])
    updated = generation_maintenance.parse_utc(row["updated_at"])
    if created is None or updated is None or created > updated:
        return False
    if row["artifacts_pruned_at"] is None:
        return True
    return generation_maintenance.parse_utc(row["artifacts_pruned_at"]) is not None


def _valid_transaction_row(row: sqlite3.Row, state: str) -> bool:
    return (
        _valid_transaction_identity(row)
        and _valid_transaction_payload(row, state)
        and _valid_transaction_timestamps(row)
    )


def _operation_shape_corrupt(
    row: sqlite3.Row, state: str, operation_positions: dict[str, list[int]]
) -> bool:
    """Whether a complete operation read contradicts this transaction's record."""
    positions = operation_positions.get(row["id"], [])
    if positions != list(range(len(positions))):
        return True
    may_own_none = state in {"preparing", "discarded"} or _never_planned(row, state)
    return not positions and not may_own_none


def _transaction_row_corrupt(
    row: sqlite3.Row, state: str, operation_positions: dict[str, list[int]]
) -> bool:
    """Corrupt on evidence only: every operation row was read, so an absent one is absent."""
    if not _valid_transaction_row(row, state):
        return True
    return _operation_shape_corrupt(row, state, operation_positions)


def _committed_within_undo_window(
    row: sqlite3.Row, state: str, cutoff: datetime
) -> bool:
    if state != "committed" or row["artifacts_pruned_at"] is not None:
        return False
    updated = generation_maintenance.parse_utc(row["updated_at"])
    return updated is not None and updated >= cutoff


def undo_artifact_retained(
    row: sqlite3.Row, state: str, cutoff: datetime, state_root: Path,
    *, artifacts: set[str] | None = None,
) -> bool:
    if not _committed_within_undo_window(row, state, cutoff):
        return False
    transaction_id = row["id"]
    if _TRANSACTION_ID_RE.fullmatch(transaction_id) is None:
        return False
    return _undo_directory_present(transaction_id, state_root, artifacts)


def _undo_directory_present(
    transaction_id: str, state_root: Path, artifacts: set[str] | None
) -> bool:
    if artifacts is not None:
        return transaction_id in artifacts
    artifact = state_root / "run" / "transactions" / transaction_id
    return generation_maintenance.safe_kind(artifact, state_root)[0] == "directory"


def _collect_error_code(
    database: sqlite3.Connection,
    row: sqlite3.Row,
    state: str,
    transaction_columns: set[str],
    codes: set[str],
) -> None:
    if state not in {"conflicted", "quarantined"} or (
        "error_code" not in transaction_columns
    ):
        return
    if row["error_code"]:
        codes.add(str(row["error_code"]))


def _scan_one_transaction_row(
    database: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    states: dict[str, int],
    details: dict,
    codes: set[str],
    operation_positions: dict[str, list[int]],
    transaction_columns: set[str],
    cutoff: datetime,
    state_root: Path,
    artifacts: set[str] | None = None,
) -> bool:
    """Count one row and report whether it is corrupt."""
    state = row["state"]
    if not isinstance(state, str) or state not in TRANSACTION_STATES:
        details["deletion_codes"].append("transaction_state_unknown")
        details["state_invalid"] = True
        return False
    states[state] += 1
    _collect_error_code(database, row, state, transaction_columns, codes)
    if undo_artifact_retained(row, state, cutoff, state_root, artifacts=artifacts):
        details["undo_retained"] += 1
    return _transaction_row_corrupt(row, state, operation_positions)


class _RowVerdict(NamedTuple):
    """What the streamed transaction scan found across every row."""

    codes: set[str]
    corrupt: bool
    artifacts_mismatched: bool


def _scan_transaction_rows(
    database: sqlite3.Connection,
    transaction_rows: Iterator[sqlite3.Row],
    operation_positions: dict[str, list[int]],
    transaction_columns: set[str],
    *,
    artifacts: set[str] | None,
    known_ids: set[str],
    state_root: Path,
    now: datetime,
    details: dict,
    states: dict[str, int],
) -> _RowVerdict:
    codes: set[str] = set()
    corrupt = mismatched = False
    cutoff = now - timedelta(days=UNDO_RETENTION_DAYS)
    for row in transaction_rows:
        corrupt = _scan_one_transaction_row(
            database,
            row,
            states=states,
            details=details,
            codes=codes,
            operation_positions=operation_positions,
            transaction_columns=transaction_columns,
            cutoff=cutoff,
            state_root=state_root,
            artifacts=artifacts,
        ) or corrupt
        mismatched = _artifact_mismatch(row, artifacts, known_ids) or mismatched
    return _RowVerdict(codes, corrupt, mismatched)


def stop_at(deadline: float) -> None:
    if monotonic_deadline.deadline_reached(deadline):
        raise TimeoutError("transaction check deadline")


def _lease_live(value: object, now: datetime) -> bool:
    return (generation_maintenance.parse_utc(value) or datetime.max.replace(tzinfo=UTC)) > now


def _count_project_leases(
    database: sqlite3.Connection, tables: set[str], details: dict, now: datetime
) -> None:
    if "project_leases" not in tables:
        return
    rows = database.execute(
        "SELECT expires_at FROM project_leases LIMIT ?", (MAX_OPERATIONAL_ROWS + 1,)
    ).fetchall()
    if len(rows) > MAX_OPERATIONAL_ROWS:
        details["deletion_codes"].append("project_lease_state_unknown")
    details["live_project_leases"] = sum(
        _lease_live(row[0], now) for row in rows[:MAX_OPERATIONAL_ROWS]
    )


def _owner_token_present(row: sqlite3.Row) -> bool:
    if "owner_token" not in row.keys():
        return True
    return bool(row["owner_token"])


def _owner_row_unknown(row: sqlite3.Row, *, require_token: bool) -> bool:
    if require_token and not _owner_token_present(row):
        return False
    return not owner_row_known(row, pid_column="process_id")


def _any_owner_row_unknown(rows: list[sqlite3.Row], require_token: bool) -> bool:
    return any(_owner_row_unknown(row, require_token=require_token) for row in rows)


def _append_owner_unknown_codes(
    details: dict,
    rows: list,
    bounded: list,
    *,
    unknown_code: str,
    require_token: bool,
) -> None:
    """Record the unknown-owner code once per reason, as the caller always did."""
    if len(rows) > MAX_OPERATIONAL_ROWS:
        details["deletion_codes"].append(unknown_code)
    if _any_owner_row_unknown(bounded, require_token):
        details["deletion_codes"].append(unknown_code)


def _count_owner_table(
    database: sqlite3.Connection,
    tables: set[str],
    details: dict,
    now: datetime,
    *,
    table: str,
    unknown_code: str,
    count_key: str,
    require_token: bool,
) -> list[sqlite3.Row]:
    if table not in tables:
        return []
    rows = database.execute(
        _OWNER_TABLE_QUERIES[table], (MAX_OPERATIONAL_ROWS + 1,)
    ).fetchall()
    bounded = rows[:MAX_OPERATIONAL_ROWS]
    _append_owner_unknown_codes(
        details, rows, bounded, unknown_code=unknown_code, require_token=require_token
    )
    details[count_key] = sum(
        generation_maintenance.live_owner(row, now, pid_column="process_id") for row in bounded
    )
    return bounded


def _overdue_owner(row: sqlite3.Row, now: datetime) -> bool:
    """A live process whose lease has run out: it holds what it promised to give up.

    A release the busy database refused is retried for about an hour and then
    given up, leaving the row of a process that is alive, so nothing reclaims it
    and every other writer waits (audit 2026-09-27 C-3,
    docs/research/2026-09-27-a-writer-past-its-lease-is-named.md).
    """
    expiry = generation_maintenance.parse_utc(generation_maintenance.row_value(row, "expires_at"))
    if expiry is None or expiry > now:
        return False
    return generation_maintenance.owner_pid_live(generation_maintenance.row_value(row, "process_id"), generation_maintenance.row_value(row, "process_start_identity"))


def _count_owner_tables(
    database: sqlite3.Connection, tables: set[str], details: dict, now: datetime
) -> None:
    _count_project_leases(database, tables, details, now)
    writers = _count_owner_table(
        database,
        tables,
        details,
        now,
        table="writer_owners",
        unknown_code="writer_state_unknown",
        count_key="live_writers",
        require_token=False,
    )
    details["overdue_writers"] = sum(_overdue_owner(row, now) for row in writers)
    _count_owner_table(
        database,
        tables,
        details,
        now,
        table="maintenance_owners",
        unknown_code="maintenance_state_unknown",
        count_key="live_maintenance_owners",
        require_token=True,
    )


def _artifact_mismatch(
    row: sqlite3.Row, artifacts: set[str] | None, known_ids: set[str]
) -> bool:
    """Whether the row and the undo directory disagree; None abstains on the directory."""
    transaction_id = row["id"]
    if not isinstance(transaction_id, str) or transaction_id not in known_ids:
        return True
    if artifacts is None:
        return False
    expected = row["state"] != "discarded" and row["artifacts_pruned_at"] is None
    return (transaction_id in artifacts) != expected


def checked_artifacts(
    state_root: Path, known_ids: set[str], details: dict, deadline: float
) -> set[str] | None:
    """The undo artifacts on disk, or None when the listing is incomplete.

    `run/transactions/` is listed under the caller's inspection deadline. With an
    incomplete scan or an unsafe entry the names read are not all there are, and a row whose directory
    was not listed looked like a row whose directory is missing - corruption
    alleged from an entry never read. The deletion refusal stays; the accusation goes.
    """
    artifacts, incomplete = transaction_artifacts(state_root, deadline)
    if incomplete or artifacts - known_ids:
        details["deletion_codes"].append("transaction_artifact_state_unknown")
    return None if incomplete else artifacts


def _mark_corrupt(details: dict) -> None:
    details["codes"].append("transaction_metadata_corrupt")
    details["deletion_codes"].append("transaction_state_corrupt")
    details["state_invalid"] = True


@contextlib.contextmanager
def one_snapshot(database: sqlite3.Connection) -> Iterator[None]:
    """The id, operation and transaction reads see one state of the database.

    The read connection autocommits, so each statement saw its own state: a
    transaction committed between the reads showed up as a row with no operations,
    or an operation of an unknown transaction, and was called corrupt. One read
    transaction holds SQLite's shared lock until the context exits. The main
    transaction check copies SQL rows here, closes its connection before file
    inspection, and revalidates those rows before accepting a verdict.
    """
    database.execute("BEGIN")
    try:
        yield
    finally:
        database.execute("COMMIT")


class _TransactionSnapshot(NamedTuple):
    transactions: tuple
    operations: tuple


def _snapshot_rows(database: sqlite3.Connection, query: str, deadline: float) -> tuple:
    rows = []
    with closing(database.execute(query)) as cursor:
        while batch := cursor.fetchmany(reliable_memory.OPERATIONAL_SCAN_BATCH_ROWS):
            stop_at(deadline)
            rows.extend(batch)
    stop_at(deadline)
    return tuple(rows)


def read_transaction_snapshot(database: sqlite3.Connection, deadline: float) -> _TransactionSnapshot:
    with one_snapshot(database):
        snapshot = _TransactionSnapshot(
            _snapshot_rows(database, 'SELECT * FROM "transaction"', deadline),
            _snapshot_rows(database, _OPERATION_QUERY, deadline),
        )
    stop_at(deadline)
    return snapshot


class _TransactionRowContribution(NamedTuple):
    states: tuple
    undo_retained: int
    state_invalid: bool
    deletion_codes: tuple
    codes: frozenset
    corrupt: bool


def _nonzero_transaction_states(states):
    return tuple((state, count) for state, count in states.items() if count)


def transaction_row_contribution(row, positions, columns, cutoff, state_root, artifacts, known_ids):
    states = {state: 0 for state in TRANSACTION_STATES}
    details = {"deletion_codes": [], "state_invalid": False, "undo_retained": 0}
    codes = set()
    corrupt = _scan_one_transaction_row(
        None, row, states=states, details=details, codes=codes,
        operation_positions=positions, transaction_columns=columns,
        cutoff=cutoff, state_root=state_root, artifacts=artifacts,
    )
    return _TransactionRowContribution(
        _nonzero_transaction_states(states), details["undo_retained"], details["state_invalid"],
        tuple(details["deletion_codes"]), frozenset(codes),
        corrupt or _artifact_mismatch(row, artifacts, known_ids),
    )


def _merge_transaction_contribution(proof, states, details, codes):
    for state, count in proof.states:
        states[state] += count
    details["undo_retained"] += proof.undo_retained
    details["state_invalid"] |= proof.state_invalid
    details["deletion_codes"].extend(proof.deletion_codes)
    codes.update(proof.codes)


class _TransactionInspection:
    """Pure row contributions belong to this inspection, never filesystem authority."""

    def __init__(self):
        self.rows = {}

    def contribution(self, row, positions, columns, cutoff, state_root, artifacts, known_ids):
        membership = None if artifacts is None else row["id"] in artifacts
        signature = (row, tuple(positions.get(row["id"], ())), membership)
        saved = self.rows.get(row["id"])
        if saved is not None and saved[0] == signature:
            return saved[1]
        proof = transaction_row_contribution(row, positions, columns, cutoff, state_root, artifacts, known_ids)
        self.rows[row["id"]] = (signature, proof)
        return proof


def _inspect_snapshot_rows(inspection, snapshot, positions, columns, artifacts, known_ids, state_root, now, deadline, details, states):
    codes = set()
    corrupt = False
    cutoff = now - timedelta(days=UNDO_RETENTION_DAYS)
    for row in _checked_snapshot_rows(snapshot.transactions, deadline):
        proof = inspection.contribution(row, positions, columns, cutoff, state_root, artifacts, known_ids)
        _merge_transaction_contribution(proof, states, details, codes)
        corrupt |= proof.corrupt
    return _RowVerdict(codes, corrupt, False)


def _snapshot_row_verdict(inspection, snapshot, positions, columns, artifacts, known_ids, state_root, now, deadline, details, states):
    if inspection is not None:
        return _inspect_snapshot_rows(inspection, snapshot, positions, columns, artifacts, known_ids, state_root, now, deadline, details, states)
    return _scan_transaction_rows(
        None, _checked_snapshot_rows(snapshot.transactions, deadline), positions, columns,
        artifacts=artifacts, known_ids=known_ids, state_root=state_root,
        now=now, details=details, states=states,
    )


def _require_reconciliation_artifacts(inspection, artifacts):
    if inspection is not None and artifacts is None:
        raise TimeoutError("transaction artifact inventory is incomplete")


def _scan_transaction_tables(
    snapshot: _TransactionSnapshot,
    transaction_columns: set[str],
    *,
    state_root: Path,
    now: datetime,
    deadline: float,
    details: dict,
    states: dict[str, int],
    inspection: _TransactionInspection | None = None,
) -> bool:
    """Inspect complete SQL rows after the read connection has been closed."""
    known_ids = {row["id"] for row in _checked_snapshot_rows(snapshot.transactions, deadline)}
    operation_positions, operations_corrupt = _operation_positions(_checked_snapshot_rows(snapshot.operations, deadline), known_ids)
    artifacts = checked_artifacts(state_root, known_ids, details, deadline)
    _require_reconciliation_artifacts(inspection, artifacts)
    verdict = _snapshot_row_verdict(
        inspection, snapshot, operation_positions, transaction_columns,
        artifacts, known_ids, state_root, now, deadline, details, states,
    )
    details["codes"] = sorted(set(details["codes"]) | verdict.codes)
    return operations_corrupt or verdict.corrupt or verdict.artifacts_mismatched


def _checked_snapshot_rows(rows: tuple, deadline: float) -> Iterator[sqlite3.Row]:
    for row in rows:
        stop_at(deadline)
        yield row


def _accept_transaction_verdict(corrupt: bool, details: dict) -> None:
    if corrupt:
        _mark_corrupt(details)


def _require_snapshot_rows(database, query, expected, deadline) -> None:
    stop_at(deadline)
    with closing(database.execute(query)) as cursor:
        offset = _matched_snapshot_rows(cursor, expected, deadline)
    stop_at(deadline)
    if offset != len(expected):
        raise TimeoutError("transaction snapshot changed during filesystem inspection")


def _matched_snapshot_rows(cursor, expected, deadline) -> int:
    offset = 0
    while batch := cursor.fetchmany(reliable_memory.OPERATIONAL_SCAN_BATCH_ROWS):
        stop_at(deadline)
        if tuple(batch) != expected[offset:offset + len(batch)]:
            raise TimeoutError("transaction snapshot changed during filesystem inspection")
        offset += len(batch)
    return offset


def require_transaction_snapshot(database, snapshot: _TransactionSnapshot, deadline: float) -> None:
    with one_snapshot(database):
        _require_snapshot_rows(database, 'SELECT * FROM "transaction"', snapshot.transactions, deadline)
        _require_snapshot_rows(database, _OPERATION_QUERY, snapshot.operations, deadline)
    stop_at(deadline)


def _snapshot_expected_row(expected, offset):
    if offset < len(expected):
        return expected[offset]
    return None


def _snapshot_identity_field(row):
    if "transaction_id" in row.keys():
        return row["transaction_id"]
    return row["id"]


def _delta_transaction_id(row):
    identifier = _snapshot_identity_field(row)
    if not isinstance(identifier, str):
        raise ValueError("transaction snapshot delta has invalid identity")
    return identifier


def _updated_snapshot_row(row, prior, changed):
    if row == prior:
        return prior
    changed.add(_delta_transaction_id(row))
    if prior is not None:
        changed.add(_delta_transaction_id(prior))
    return row


def _refresh_snapshot_rows(database, query, expected, deadline, changed):
    rows = []
    with closing(database.execute(query)) as cursor:
        for row in cursor:
            stop_at(deadline)
            rows.append(_updated_snapshot_row(row, _snapshot_expected_row(expected, len(rows)), changed))
    stop_at(deadline)
    for row in expected[len(rows):]:
        changed.add(_delta_transaction_id(row))
    return tuple(rows)


def _require_reconciliation_schema(database, schemas, deadline):
    current = _transaction_schema(database, database_tables(database, deadline), deadline)
    if current != schemas:
        raise TimeoutError("transaction snapshot schema changed during filesystem inspection")


def _refresh_transaction_snapshot(database, snapshot, deadline, schemas):
    changed = set()
    with one_snapshot(database):
        _require_reconciliation_schema(database, schemas, deadline)
        current = _TransactionSnapshot(
            _refresh_snapshot_rows(database, 'SELECT * FROM "transaction"', snapshot.transactions, deadline, changed),
            _refresh_snapshot_rows(database, _OPERATION_QUERY, snapshot.operations, deadline, changed),
        )
    stop_at(deadline)
    return current, changed


def _terminal_transaction_rows(snapshot):
    terminal = set(TRANSACTION_STATES) - set(UNSETTLED_TRANSACTION_STATES)
    return {row["id"]: row for row in snapshot.transactions if row["state"] in terminal}


def _require_prior_terminal_row(row, terminal):
    previous = terminal.get(row["id"])
    if previous is not None and row != previous:
        raise TimeoutError("immutable terminal transaction changed during filesystem inspection")


def _terminal_operation_rows(snapshot, terminal):
    return tuple(row for row in snapshot.operations if row["transaction_id"] in terminal)


def _require_terminal_evidence_unchanged(previous, current):
    terminal = _terminal_transaction_rows(previous)
    for row in current.transactions:
        _require_prior_terminal_row(row, terminal)
    if _terminal_operation_rows(previous, terminal) != _terminal_operation_rows(current, terminal):
        raise TimeoutError("immutable terminal operations changed during filesystem inspection")


def _require_terminal_delta(snapshot, changed, details):
    states = {row["id"]: row["state"] for row in snapshot.transactions}
    terminal = set(TRANSACTION_STATES) - set(UNSETTLED_TRANSACTION_STATES)
    if any(states.get(identifier) not in terminal for identifier in changed):
        raise TimeoutError("transaction snapshot changed during filesystem inspection")
    _require_no_live_delta_writer(details)


def _require_no_live_delta_writer(details):
    if details["live_writers"] or details["overdue_writers"]:
        raise TimeoutError("transaction snapshot changed while writer remains live")


def finish_reconciled_snapshot(path, state_root, now, deadline, details, snapshot, schemas, vault_root):
    with closing(doctor_base.readonly_database(path, state_root, deadline=deadline)) as database:
        _count_owner_tables(database, database_tables(database, deadline), details, now)
        details["quarantined_unresolved"] = unresolved_quarantine(
            database, schemas[0],
            CompiledDaySupersession(vault_root, state_root, database=database, deadline=deadline),
        )
        return _refresh_transaction_snapshot(database, snapshot, deadline, schemas)


def _replace_transaction_details(details, states):
    current, counts = empty_transaction_details()
    states.clear()
    states.update(counts)
    current["states"] = states
    details.clear()
    details.update(current)


def _require_final_reconciliation_artifacts(details):
    if "transaction_artifact_state_unknown" in details["deletion_codes"]:
        raise TimeoutError("transaction artifact inventory has unknown authority")


def _reconcile_transaction_tables(path, snapshot, schemas, state_root, now, deadline, details, states, vault_root):
    inspection = _TransactionInspection()
    while True:
        stop_at(deadline)
        _replace_transaction_details(details, states)
        corrupt = _scan_transaction_tables(
            snapshot, schemas[0], state_root=state_root, now=now, deadline=deadline,
            details=details, states=states, inspection=inspection,
        )
        current, changed = finish_reconciled_snapshot(path, state_root, now, deadline, details, snapshot, schemas, vault_root)
        if not changed:
            _require_final_reconciliation_artifacts(details)
            _accept_transaction_verdict(corrupt, details)
            return
        _require_terminal_delta(current, changed, details)
        _require_terminal_evidence_unchanged(snapshot, current)
        snapshot = current


def _operation_columns(
    database: sqlite3.Connection, tables: set[str], deadline: float
) -> set[str]:
    if "operation" not in tables:
        return set()
    return generation_maintenance.database_table_columns(database, "operation", deadline)


def _transaction_schema_complete(
    transaction_columns: set[str], operation_columns: set[str]
) -> bool:
    return TRANSACTION_REQUIRED_COLUMNS.issubset(
        transaction_columns
    ) and OPERATION_REQUIRED_COLUMNS.issubset(operation_columns)


def _transaction_schema(
    database: sqlite3.Connection, tables: set[str], deadline: float
) -> tuple[set[str], set[str]]:
    """The transaction and operation columns, refusing an absent table."""
    if "transaction" not in tables:
        raise sqlite3.DatabaseError("transaction table missing")
    return (
        generation_maintenance.database_table_columns(database, "transaction", deadline),
        _operation_columns(database, tables, deadline),
    )


def scan_transaction_database(
    path: Path,
    state_root: Path,
    now: datetime,
    deadline: float,
    details: dict,
    states: dict[str, int],
    *,
    vault_root: Path | None = None,
) -> dict | None:
    """Fill in the counters; return a result only when the schema is incomplete."""
    with closing(doctor_base.readonly_database(path, state_root, deadline=deadline)) as database:
        if monotonic_deadline.deadline_reached(deadline):
            raise TimeoutError("transaction check deadline")
        tables = database_tables(database, deadline)
        transaction_columns, operation_columns = _transaction_schema(
            database, tables, deadline
        )
        if not _transaction_schema_complete(transaction_columns, operation_columns):
            details["codes"].append("transaction_metadata_missing")
            details["deletion_codes"].append("transaction_state_corrupt")
            return generation_maintenance.result(
                "transactions",
                "error",
                "Transaction metadata is incomplete.",
                details,
            )
        snapshot = read_transaction_snapshot(database, deadline)
    _reconcile_transaction_tables(path, snapshot, (transaction_columns, operation_columns), state_root, now, deadline, details, states, vault_root)
    return None


def _quarantined_ids(database: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in database.execute(
            'SELECT id FROM "transaction" WHERE state=\'quarantined\''
        )
    }


_COMPILE_RECEIPT_PREFIX = "knowledge/daily/receipts/v3-"
_COMPILE_RECEIPT_PREFIXES = (_COMPILE_RECEIPT_PREFIX, "knowledge/daily/receipts/v4-")
_STAGED_ARTIFACT_RE = re.compile(r"after/[0-9]{6}\.bin")
_DAILY_LOGICAL_PATH_RE = re.compile(r"knowledge/daily/[0-9]{4}-[0-9]{2}-[0-9]{2}\.md")
_RECEIPT_RECORD_RE = re.compile(rb"(?s)```json\n(.*?)\n```")


# Reading a quarantined compile's staged files to see whether a later compile
# superseded it; a file past its bound makes the check answer "not superseded", never
# a guess. The receipt and day bounds are compile_receipt_authority.MAX_RECEIPT_BYTES and
# the shared evidence_resolver.MAX_DAILY_BYTES source-family contract. The
# largest of 5 537 staged plans on the installed vault on 2026-09-27 was 2 758 bytes,
# so 4 MiB for a plan is a guard, not a fit.
_MAX_STAGED_PLAN_BYTES = 4 * 1024 * 1024
_MAX_STAGED_RECEIPT_BYTES = 1024 * 1024


def _intended_creates(database: sqlite3.Connection, identifier: str) -> set[str]:
    return {
        row[0]
        for row in database.execute(
            "SELECT path FROM operation WHERE transaction_id = ? AND kind = 'create'",
            (identifier,),
        )
    }


def _only_compile_receipts(paths: set[str]) -> bool:
    return bool(paths) and all(path.startswith(_COMPILE_RECEIPT_PREFIXES) for path in paths)


def _mapping_field(value: object, key: str) -> object:
    if isinstance(value, dict):
        return value.get(key)
    return None


def _daily_logical_path(value: object) -> str | None:
    if isinstance(value, str) and _DAILY_LOGICAL_PATH_RE.fullmatch(value) is not None:
        return value
    return None


def _staged_receipt_day(raw: bytes) -> str | None:
    """The day a staged compile receipt names, or None when it is not one."""
    match = _RECEIPT_RECORD_RE.search(raw)
    if match is None:
        return None
    source = _mapping_field(json.loads(match[1]), "source")
    return _daily_logical_path(_mapping_field(source, "logical_path"))


class _ReceiptOperation(NamedTuple):
    path: str
    kind: str
    after_hash: str


def _snapshot_receipt_fields(raw: bytes, path: str) -> tuple[str, str]:
    from llm_wiki.formats.receipts import parse_compile_receipt_version
    from llm_wiki.formats.receipts import receipt_source_fields as _receipt_source_fields

    fields = _receipt_source_fields(raw)
    if fields is None:
        raise ValueError("staged receipt lacks source identity")
    record = parse_compile_receipt_version(raw, logical_path=fields[0], source_sha256=fields[1])
    version = record["schema_version"].rsplit("/", 1)[1]
    if path != f"knowledge/daily/receipts/{version}-{record['source_identity']}.md":
        raise ValueError("staged receipt path disagrees with source identity")
    return fields


def committed_snapshot_receipt(database, path: str, raw: bytes, fields, *, require_create: bool = False) -> bool:
    from llm_wiki.compile_receipt_authority import require_operation_integrity
    from llm_wiki.formats.receipts import parse_compile_receipt_version

    record = parse_compile_receipt_version(raw, logical_path=fields[0], source_sha256=fields[1])
    row = database.execute(
        'SELECT t.id FROM "transaction" t JOIN operation o ON o.transaction_id=t.id '
        "WHERE t.state='committed' AND o.path=? AND o.after_hash=? AND (?=0 OR o.kind='create') "
        "AND (t.operation_id=? OR t.operation_id LIKE ?) ORDER BY t.created_at DESC LIMIT 1",
        (path, hashlib.sha256(raw).hexdigest(), require_create, record["operation_id"], record["operation_id"] + "#%"),
    ).fetchone()
    if row is None:
        return False
    operations = {
        item[0]: _ReceiptOperation(*item)
        for item in database.execute(
            "SELECT path,kind,after_hash FROM operation WHERE transaction_id=?", (row[0],)
        )
    }
    require_operation_integrity(record, operations)
    return True


def _staged_snapshot_receipt(directory: Path, operation: dict, state_root: Path) -> bytes:
    from markdown_transaction import decoded_image_bytes

    after = operation["after"]
    artifact = after["artifact"]
    if _STAGED_ARTIFACT_RE.fullmatch(artifact) is None:
        raise ValueError("staged receipt path is not a plain after artifact")
    encoded = read_runtime_bytes(directory / artifact, state_root, max_bytes=_MAX_STAGED_RECEIPT_BYTES)
    raw = decoded_image_bytes(encoded, max_bytes=_MAX_STAGED_RECEIPT_BYTES)
    if hashlib.sha256(raw).hexdigest() != after["sha256"]:
        raise ValueError("staged receipt image hash differs")
    return raw


def _snapshot_receipt_written(database, vault_root, state_root, directory, operation, digest) -> bool:
    path = operation["path"]
    if operation["after"]["sha256"] != digest:
        raise ValueError("staged receipt hash disagrees with transaction")
    staged = _staged_snapshot_receipt(directory, operation, state_root)
    fields = _snapshot_receipt_fields(staged, path)
    raw = read_stable_bytes(vault_root / path, _MAX_STAGED_RECEIPT_BYTES, label="compile receipt")
    if hashlib.sha256(raw).hexdigest() != digest:
        return False
    return committed_snapshot_receipt(database, path, raw, fields)


def _intended_compile_receipts(database, identifier) -> dict[str, str]:
    return {
        item[0]: item[1] for item in database.execute(
            "SELECT path,after_hash FROM operation WHERE transaction_id=? AND kind='create'",
            (identifier,),
        ) if item[0].startswith(_COMPILE_RECEIPT_PREFIXES)
    }


def _compile_snapshot_outcome(database, identifier, vault_root, state_root) -> bool:
    row = database.execute('SELECT operation_id FROM "transaction" WHERE id=?', (identifier,)).fetchone()
    if row is None or not row[0].startswith("compile:"):
        return False
    receipts = _intended_compile_receipts(database, identifier)
    if not receipts:
        return False
    return _compile_snapshot_receipts_written(database, identifier, vault_root, state_root, receipts)


def _staged_receipt_operations(plan, receipts) -> dict:
    staged = {item["path"]: item for item in plan["operations"] if item["path"] in receipts}
    if set(staged) != set(receipts):
        raise ValueError("refused compile does not stage every bound receipt")
    return staged


def _compile_snapshot_receipts_written(database, identifier, vault_root, state_root, receipts) -> bool:
    directory, plan = _verified_compile_plan(database, identifier, state_root)
    staged = _staged_receipt_operations(plan, receipts)
    return all(
        _snapshot_receipt_written(database, vault_root, state_root, directory, staged[path], digest)
        for path, digest in receipts.items()
    )


def _verified_compile_plan(database, identifier, state_root):
    directory = state_root / "run" / "transactions" / identifier
    raw = read_runtime_bytes(directory / "plan.json", state_root, max_bytes=_MAX_STAGED_PLAN_BYTES)
    row = database.execute('SELECT plan_hash FROM "transaction" WHERE id=?', (identifier,)).fetchone()
    if row is None or hashlib.sha256(raw).hexdigest() != row["plan_hash"]:
        raise ValueError("staged plan hash differs from canonical transaction")
    plan = json.loads(raw)
    if plan["transaction_id"] != identifier:
        raise ValueError("staged plan transaction identity differs")
    return directory, plan


def compile_snapshot_was_written(database, identifier, vault_root, state_root) -> bool:
    if vault_root is None:
        return False
    try:
        return _compile_snapshot_outcome(database, identifier, vault_root, state_root)
    except (OSError, ValueError, KeyError, TypeError, lzma.LZMAError):
        return False


class _DoctorCompileAuthority(MarkdownCoordinator):
    """Borrow doctor's read-only connection for the canonical authority reader."""

    def __init__(self, vault_root, state_root, database):
        self.vault = Path(vault_root)
        self.state_root = Path(state_root)
        self.database = database

    @contextlib.contextmanager
    def _connect(self, *, busy_ms=None, deadline=None):
        yield self.database


def _historical_receipt_record(raw, path):
    from llm_wiki.formats.receipts import parse_compile_receipt_version

    fields = _snapshot_receipt_fields(raw, path)
    record = parse_compile_receipt_version(raw, logical_path=fields[0], source_sha256=fields[1])
    if record["schema_version"] != "compile-receipt/v3":
        raise ValueError("historical source outcome requires a legacy v3 receipt")
    if record["source"] not in record["batch_manifest"]:
        raise ValueError("historical source lacks a complete terminal batch binding")
    return record


def _same_historical_source(previous, current):
    return (previous["source"] == current["source"]
            and previous["batch_manifest"] == current["batch_manifest"])


def _require_historical_request(previous, identity):
    if previous["operation_id"] != identity:
        raise ValueError("staged receipt disagrees with the historical compile request")


def _historical_compile_identity(database, identifier):
    row = database.execute('SELECT operation_id,state FROM "transaction" WHERE id=?', (identifier,)).fetchone()
    if row is None or row["state"] != "quarantined":
        raise ValueError("historical compile requires a retained quarantined attempt")
    identity = base_operation_identity(row["operation_id"])
    if not identity.startswith("compile:"):
        raise ValueError("historical outcome is not a compile attempt")
    return identity


class _HistoricalDailyLayout:
    """Replay part payloads from one proof-owned immutable source."""

    def __init__(self, logical, source, frames):
        from bounded_io import immutable_source_digest
        from evidence_resolver import daily_entries, daily_part_bounds

        self.logical, self.source, self.frames = logical, source, frames
        self.bounds = daily_part_bounds(source)
        self.entries = tuple(daily_entries(source))
        self.digest = immutable_source_digest(source)
        self.unit_index = None

    def __iter__(self):
        self.source.require_active()
        for index in range(len(self.bounds)):
            yield self.part(index)

    def part(self, index):
        from compile_receipts import (
            daily_part_record,
        )

        return daily_part_record(self.logical, self.source, self.bounds, index,
                                  self.digest, self.entries, self.frames)

    def matching_units(self, source):
        self.source.require_active()
        if self.unit_index is None:
            self.unit_index = _historical_unit_index(self)
        key = source["byte_size"], source["sha256"]
        return (_HistoricalUnit(self, indexes) for indexes in self.unit_index.get(key, ()))


class _HistoricalUnit(Sequence):
    def __init__(self, layout, indexes):
        self.layout, self.indexes = layout, indexes

    def __len__(self):
        self.layout.source.require_active()
        return len(self.indexes)

    def __getitem__(self, index):
        self.layout.source.require_active()
        if isinstance(index, slice):
            return _HistoricalUnit(self.layout, self.indexes[index])
        return self.layout.part(self.indexes[index])


def _historical_unit_index(layout):
    from llm_wiki.compile_pipeline_sources import (
        native_part_units,
    )

    index = {}
    for unit in native_part_units(layout):
        key = _historical_unit_key(unit)
        index.setdefault(key, []).append(tuple(part.part_index for part in unit))
    return index


def _historical_unit_key(unit):
    digest = hashlib.sha256()
    for part in unit:
        digest.update(part.content)
    return sum(len(part.content) for part in unit), digest.hexdigest()


class CurrentHistoricalSourceWork:
    """Exact new source work, without granting historical offset authority."""

    def __init__(self, reader, previous):
        self.reader = reader
        self.previous = previous
        self.captured = {}
        self.layouts = {}
        self.units = []
        self.selector = reader._cached_selection()
        self.owner = None

    def __enter__(self):
        self.reader._require_active()
        if self.owner is not None:
            raise ValueError("historical proof already owns an active scope")
        self.owner = contextlib.ExitStack()
        return self

    def __exit__(self, *exception):
        try:
            return self.owner.__exit__(*exception)
        finally:
            self.owner = None
            self.selector.prefix_source = None
            self.captured.clear()
            self.layouts.clear()
            self.units.clear()

    def verified(self):
        with self:
            return self._verify_current_work()

    def _verify_current_work(self):
        for source in self.previous["batch_manifest"]:
            self.units.append(self._matching_unit(source))
        descriptors = self._descriptors()
        records = self._records()
        if not _all_current_work_records(records):
            return False
        if not self._witnesses_hold(records, descriptors):
            return False
        self._recheck_sources()
        return self._records() == records and self._witnesses_hold(records, descriptors)

    def _records(self):
        return [self.selector.receipt(part) for unit in self.units for part in unit]

    def _witnesses_hold(self, records, descriptors):
        witnesses = [record for record in records if record["batch_manifest"] == descriptors]
        return bool(witnesses) and all(self._witness(record) for record in witnesses)

    def _capture(self, logical):
        from llm_wiki.compile_pipeline_sources import (
            native_daily_frames,
        )

        self.reader._require_active()
        self._require_source_scope()
        if logical in self.layouts:
            return self.layouts[logical]
        raw = self.owner.enter_context(self.reader._open_daily(logical))
        with self.reader._breadcrumb_record_parsing():
            frames = native_daily_frames(logical, raw, self.reader.vault_root)
            layout = _HistoricalDailyLayout(logical, raw, frames)
        self.reader._require_active()
        self.captured[logical] = raw
        self.layouts[logical] = layout
        return self.layouts[logical]

    def _require_source_scope(self):
        if self.owner is None:
            raise ValueError("historical capture requires an active source scope")

    def _matching_unit(self, source):
        layout = self._capture(source["logical_path"])
        matches = [unit for unit in layout.matching_units(source) if self._unit_matches(unit, source)]
        if len(matches) != 1:
            raise ValueError("historical source lacks one complete current source unit")
        return matches[0]

    def _unit_matches(self, unit, source):
        from llm_wiki.compile_pipeline_sources import (
            native_unit_source,
            source_descriptor,
        )

        self.reader._require_active()
        if sum(len(part.content) for part in unit) != source["byte_size"]:
            return False
        snapshot = native_unit_source(unit)
        expected = source_descriptor(snapshot).receipt_descriptor()
        if expected != source:
            return False
        return self._unique_containment(unit, snapshot.content)

    def _unique_containment(self, unit, selected):
        raw = self.captured[unit[0].logical_path]
        start = raw.find(selected)
        return start == unit[0].byte_start and raw.find(selected, start + 1) == -1

    def _descriptors(self):
        from llm_wiki.compile_pipeline_receipts import (
            v4_source_descriptor,
        )

        sources = [v4_source_descriptor(part) for unit in self.units for part in unit]
        return sorted(sources, key=_current_work_descriptor_key)

    def _witness(self, record):
        self.reader._require_active()
        path = f"knowledge/daily/receipts/v4-{record['source_identity']}.md"
        raw = self.reader._historical_receipt_bytes(path)
        fields = _snapshot_receipt_fields(raw, path)
        _require_current_work_record(raw, fields, record)
        if not committed_snapshot_receipt(self.reader.database, path, raw, fields, require_create=True):
            return False
        self._live_outputs(record)
        return self.reader._historical_receipt_bytes(path) == raw

    def _live_outputs(self, record):
        from llm_wiki.compile_pipeline_base import (
            MAX_AFTER_IMAGE_BYTES,
        )

        for operation in record["operations"]:
            self.reader._require_active()
            path = self.reader.vault_root / operation["path"]
            if generation_maintenance.safe_kind(path, self.reader.vault_root)[0] != "regular":
                raise ValueError("current historical output is unavailable")
            raw = read_stable_bytes(path, MAX_AFTER_IMAGE_BYTES, label="compile output")
            if hashlib.sha256(raw).hexdigest() != operation["after_sha256"]:
                raise ValueError("current historical output changed")

    def _recheck_sources(self):
        from llm_wiki.compile_pipeline_sources import (
            native_unit_source,
        )

        for source in self.captured.values():
            self.reader._require_active()
            source.require_active()
        for unit in self.units:
            self.reader._require_active()
            native_unit_source(unit)
        self.reader._require_active()


def _current_work_descriptor_key(source):
    return source["logical_path"], source["byte_start"], source["byte_end"]


def _all_current_work_records(records):
    return bool(records) and all(records)


def _require_current_work_record(raw, fields, expected):
    from llm_wiki.formats.receipts import parse_compile_receipt_v4

    record = parse_compile_receipt_v4(raw, logical_path=fields[0], source_sha256=fields[1])
    if record != expected:
        raise ValueError("current historical witness changed during verification")


class CompiledDaySupersession:
    """Read-only outcomes for refused compiles; quarantine remains retained.

    A different committed immutable v3 receipt can prove the exact historical
    source descriptor and complete batch manifest. That proof does not select
    the current day, promote v3 to v4 authority, or grant deletion permission.

    A current committed v4 batch may separately prove new work on those exact
    old bytes, only as complete current units with a matching full batch and
    live output integrity. Current physical offsets never become v3 authority.

    A refused attempt that meant to create only compile receipts is history
    when every day its staged receipts name is compiled as it stands today:
    each part of the day's current bytes has a committed receipt. The refused
    snapshots' own receipts can never appear, because a day is only ever
    compiled at its current bytes (docs/research/2026-09-11-a-refused-compile-of-a-day-since-compiled-is-history.md).
    Anything unreadable or unexpected leaves the finding in place.
    """

    def __init__(self, vault_root: Path | None, state_root: Path, *, database=None, deadline=math.inf) -> None:
        self.vault_root = vault_root
        self.state_root = state_root
        self.database = database
        self.deadline = deadline
        self.selection = None
        self.breadcrumb_parsing = None

    def _breadcrumb_record_parsing(self):
        from breadcrumb_protocol import RecordParsing, using_record_parsing

        self._require_active()
        if self.breadcrumb_parsing is None:
            self.breadcrumb_parsing = RecordParsing(self._require_active)
        return using_record_parsing(self.breadcrumb_parsing)

    def resolves(self, database: sqlite3.Connection, identifier: str, committed_creates: set[str]) -> bool:
        self._require_active()
        self.database = database
        if self._exact_or_source_outcome(identifier):
            return True
        intended = _intended_creates(database, identifier)
        if self.vault_root is None or not _only_compile_receipts(intended):
            return False
        try:
            return self._staged_days_compiled(identifier, intended, committed_creates)
        except (OSError, ValueError, KeyError, TypeError, lzma.LZMAError):
            self._require_active()
            return False

    def _exact_or_source_outcome(self, identifier):
        return compile_snapshot_was_written(
            self.database, identifier, self.vault_root, self.state_root
        ) or self._historical_source_outcome(identifier) or self._current_historical_outcome(identifier) or self._source_context_outcome(identifier)

    def _current_historical_outcome(self, identifier):
        if self.vault_root is None:
            return False
        try:
            identity = _historical_compile_identity(self.database, identifier)
            receipts = _intended_compile_receipts(self.database, identifier)
            directory, plan = _verified_compile_plan(self.database, identifier, self.state_root)
            staged = _staged_receipt_operations(plan, receipts)
            return bool(receipts) and all(self._current_staged_outcome(directory, staged[path], digest, identity)
                                          for path, digest in receipts.items())
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, lzma.LZMAError):
            self._require_active()
            return False

    def _current_staged_outcome(self, directory, operation, digest, identity):
        self._require_active()
        if operation["after"]["sha256"] != digest:
            raise ValueError("staged receipt hash disagrees with transaction")
        staged = _staged_snapshot_receipt(directory, operation, self.state_root)
        previous = _historical_receipt_record(staged, operation["path"])
        _require_historical_request(previous, identity)
        return CurrentHistoricalSourceWork(self, previous).verified()

    def _historical_source_outcome(self, identifier):
        """A different committed v3 receipt can prove the exact historical work.

        This does not select the current day or alter the quarantined record.
        Both complete source descriptors and all batch members must agree.
        """
        if self.vault_root is None:
            return False
        try:
            identity = _historical_compile_identity(self.database, identifier)
            receipts = _intended_compile_receipts(self.database, identifier)
            directory, plan = _verified_compile_plan(self.database, identifier, self.state_root)
            staged = _staged_receipt_operations(plan, receipts)
            return bool(receipts) and all(
                self._historical_receipt_outcome(directory, staged[path], digest, identity)
                for path, digest in receipts.items()
            )
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, lzma.LZMAError):
            self._require_active()
            return False

    def _historical_receipt_outcome(self, directory, operation, digest, identity):
        self._require_active()
        if operation["after"]["sha256"] != digest:
            raise ValueError("staged receipt hash disagrees with transaction")
        path = operation["path"]
        staged = _staged_snapshot_receipt(directory, operation, self.state_root)
        previous = _historical_receipt_record(staged, path)
        _require_historical_request(previous, identity)
        raw = self._historical_receipt_bytes(path)
        current = _historical_receipt_record(raw, path)
        if not _same_historical_source(previous, current):
            return False
        fields = (current["source"]["logical_path"], current["source"]["sha256"])
        committed = committed_snapshot_receipt(self.database, path, raw, fields, require_create=True)
        return committed and self._historical_receipt_bytes(path) == raw

    def _historical_receipt_bytes(self, path):
        self._require_active()
        raw = read_stable_bytes(self.vault_root / path, _MAX_STAGED_RECEIPT_BYTES, label="compile receipt")
        self._require_active()
        return raw

    def _source_context_outcome(self, identifier):
        if self.vault_root is None:
            return False
        try:
            manifests = self._staged_context_manifests(identifier)
            return bool(manifests) and all(self._manifest_compiled(item) for item in manifests)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, lzma.LZMAError):
            self._require_active()
            return False

    def _staged_context_manifests(self, identifier):
        row = self.database.execute('SELECT operation_id FROM "transaction" WHERE id=?', (identifier,)).fetchone()
        if row is None or not row["operation_id"].startswith("compile:"):
            return []
        receipts = _intended_compile_receipts(self.database, identifier)
        if not receipts:
            return []
        directory, plan = _verified_compile_plan(self.database, identifier, self.state_root)
        staged = _staged_receipt_operations(plan, receipts)
        return [self._staged_manifest(directory, staged[path], digest) for path, digest in receipts.items()]

    def _staged_manifest(self, directory, operation, digest):
        from llm_wiki.formats.receipts import parse_compile_receipt_version

        self._require_active()
        if operation["after"]["sha256"] != digest:
            raise ValueError("staged receipt hash disagrees with transaction")
        raw = _staged_snapshot_receipt(directory, operation, self.state_root)
        fields = _snapshot_receipt_fields(raw, operation["path"])
        record = parse_compile_receipt_version(raw, logical_path=fields[0], source_sha256=fields[1])
        return record["schema_version"], record["batch_manifest"]

    def _manifest_compiled(self, item):
        version, sources = item
        readers = {"compile-receipt/v3": self._whole_legacy_source_compiled,
                   "compile-receipt/v4": self._saved_context_source_compiled}
        return all(readers[version](source) for source in sources)

    def _saved_context_source_compiled(self, source):
        self._require_active()
        return self._cached_selection().matches_saved_source(source)

    def _whole_legacy_source_compiled(self, source):
        from bounded_io import immutable_source_digest

        self._require_active()
        logical = source["logical_path"]
        with self._open_daily(logical) as content:
            if immutable_source_digest(content) != source["sha256"]:
                return False
            return self._daily_content_compiled(logical, content)

    def _staged_days_compiled(self, identifier: str, intended: set[str], committed_creates: set[str]) -> bool:
        days = self._staged_days(identifier, intended)
        return bool(days) and all(self._day_compiled(day, committed_creates) for day in days)

    def _staged_days(self, identifier: str, intended: set[str]) -> set[str]:
        directory, plan = _verified_compile_plan(self.database, identifier, self.state_root)
        staged = {
            operation["path"]: operation["after"]["artifact"]
            for operation in plan["operations"]
            if operation["path"] in intended
        }
        if set(staged) != intended:
            raise ValueError("refused attempt does not stage every receipt it names")
        return {self._staged_day(directory, artifact) for artifact in staged.values()}

    def _staged_day(self, directory: Path, artifact: str) -> str:
        if _STAGED_ARTIFACT_RE.fullmatch(artifact) is None:
            raise ValueError("staged artifact path is not a plain after/ artifact")
        from markdown_transaction import decoded_image_bytes

        encoded = read_runtime_bytes(directory / artifact, self.state_root, max_bytes=_MAX_STAGED_RECEIPT_BYTES)
        raw = decoded_image_bytes(encoded, max_bytes=_MAX_STAGED_RECEIPT_BYTES)
        day = _staged_receipt_day(raw)
        if day is None:
            raise ValueError("staged artifact is not a compile receipt for a day")
        return day

    def _day_compiled(self, logical_path: str, committed_creates: set[str]) -> bool:
        self._require_active()
        with self._open_daily(logical_path) as content:
            return self._daily_content_compiled(logical_path, content)

    def _open_daily(self, logical_path):
        deadline = None if self.deadline == math.inf else self.deadline
        return open_stable_range(
            self.vault_root / logical_path, MAX_DAILY_BYTES, label="daily source",
            deadline=deadline,
        )

    def _daily_content_compiled(self, logical_path, content):
        from compile_pipeline import (
            daily_is_compiled,
        )

        if self.database is not None:
            return daily_is_compiled(logical_path, content, self._cached_selection())
        return self._standalone_day_compiled(logical_path, content)

    def _require_active(self):
        if monotonic_deadline.deadline_reached(self.deadline):
            raise TimeoutError("compile context authority deadline exceeded")

    def _new_selection(self, database):
        from compile_receipts import (
            receipt_predicate,
        )

        authority = _DoctorCompileAuthority(self.vault_root, self.state_root, database)
        return receipt_predicate(authority, deadline=self.deadline)

    def _cached_selection(self):
        if self.selection is None:
            self.selection = self._new_selection(self.database)
        return self.selection

    def _standalone_day_compiled(self, logical_path, content):
        from compile_pipeline import (
            daily_is_compiled,
        )

        path = operational_database_path(self.state_root, "coordinator")
        with closing(doctor_base.readonly_database(path, self.state_root, deadline=self.deadline)) as database:
            snapshot = read_transaction_snapshot(database, self.deadline)
        with closing(doctor_base.readonly_database(path, self.state_root, deadline=self.deadline)) as database:
            compiled = daily_is_compiled(logical_path, content, self._new_selection(database))
            require_transaction_snapshot(database, snapshot, self.deadline)
        return compiled


def unresolved_quarantine(
    database: sqlite3.Connection,
    transaction_columns: set[str],
    supersession: CompiledDaySupersession | None = None,
) -> int:
    """Quarantined attempts whose work never happened.

    Quarantine is retained evidence, so counting all of it as an open problem
    left a vault that had already recovered permanently reporting `error` — and
    a health check that is always red stops being read, which is the opposite of
    why it exists.

    An attempt is history on any of three proofs. A retry in its own chain
    committed — that is the lineage. Or a commit carries the same operation
    identity under a retry ordinal — that is the same lineage written in the
    identity, and it is what an append race refused before the parent column
    was populated leaves behind. Or everything it meant to create was
    created by a transaction that did commit — that is the outcome, and it is
    the ordinary case: once the refusal is fixed the new attempt legitimately
    carries a different operation identity, because its inputs or dispositions
    changed with the fix. An attempt that intended no creation, or whose pages
    were never written, stays a finding: there something really was lost.
    """
    if "parent_transaction_id" not in transaction_columns:
        return _quarantined_total(database)
    open_attempts = _quarantined_ids(database) - resolved_by_lineage(database)
    if not open_attempts:
        return 0
    committed_creates = committed_created_paths(database)
    return sum(
        1
        for identifier in open_attempts
        if not attempt_is_history(database, identifier, committed_creates, supersession)
    )


def attempt_is_history(
    database: sqlite3.Connection,
    identifier: str,
    committed_creates: set[str],
    supersession: CompiledDaySupersession | None,
) -> bool:
    if outcome_was_written(database, identifier, committed_creates):
        return True
    return supersession is not None and supersession.resolves(database, identifier, committed_creates)


def _quarantined_total(database: sqlite3.Connection) -> int:
    row = database.execute(
        'SELECT COUNT(*) FROM "transaction" WHERE state=\'quarantined\''
    ).fetchone()
    return int(row[0]) if row else 0


def _unreadable_transactions(details: dict, message: str) -> dict:
    details["read_error"] = True
    details["deletion_codes"].append("transaction_state_unreadable")
    return generation_maintenance.result("transactions", "error", message, details)


def _missing_transaction_database(
    path: Path, state_root: Path, details: dict, deadline: float
) -> dict:
    artifacts, unsafe = transaction_artifacts(state_root, deadline)
    if artifacts or unsafe or database_sidecar_present(path, state_root):
        return _unreadable_transactions(
            details, "Transaction artifacts lack readable state."
        )
    return generation_maintenance.result("transactions", "ok", "No transaction database exists.", details)


def _transaction_status(
    states: dict[str, int], problem: int, invalid: bool, unresolved: int = 0
) -> str:
    if states["conflicted"] or unresolved or invalid:
        return "error"
    if problem:
        return "degraded"
    return "ok"


def _append_state_deletion_codes(details: dict, states: dict[str, int]) -> None:
    nonterminal = any(
        states.get(state, 0) for state in UNSETTLED_TRANSACTION_STATES
    )
    ordered = (
        (nonterminal, "transaction_nonterminal"),
        (states["conflicted"], "transaction_conflicted"),
        (states["quarantined"], "transaction_quarantined"),
    )
    for flagged, code in ordered:
        if flagged:
            details["deletion_codes"].append(code)


def _append_live_deletion_codes(details: dict) -> None:
    for key, code in (
        ("undo_retained", "transaction_undo_retained"),
        ("live_project_leases", "project_lease_live"),
        ("live_writers", "writer_live"),
        ("live_maintenance_owners", "maintenance_owner_live"),
    ):
        if details[key]:
            details["deletion_codes"].append(code)


def unsettled_count(states: dict[str, int]) -> int:
    return (
        sum(states[state] for state in UNSETTLED_TRANSACTION_STATES)
        + states["conflicted"]
    )


def _attention_parts(states: dict[str, int], invalid_state: bool, details: dict) -> list[str]:
    unsettled = unsettled_count(states)
    candidates = (
        (unsettled, f"{unsettled} transaction(s) still unsettled"),
        (
            details["quarantined_unresolved"],
            f"{details['quarantined_unresolved']} refused attempt(s) whose work never happened",
        ),
        (invalid_state, "a transaction in a state this runtime does not define"),
        (
            details["overdue_writers"],
            f"{details['overdue_writers']} writer(s) holding the writer gate past its lease "
            "while their process lives, so every other writer waits",
        ),
    )
    return [text for present, text in candidates if present]


def transaction_message(
    states: dict[str, int], problem: int, invalid_state: bool, details: dict
) -> str:
    """Say which thing needs attention, because "attention" is not an instruction.

    The line a person reads said only that something wanted looking at, and the
    counts were in the details nobody opens. Nine of the nine refused attempts
    on this vault are a breadcrumb line and one blocked compile — worth naming,
    because an operator who reads "requires attention" every day and finds the
    same nine stops reading the line at all.
    """
    if not (problem or invalid_state):
        return "Transaction state is healthy."
    parts = _attention_parts(states, invalid_state, details)
    return "Transaction state requires operator attention: " + "; ".join(parts) + "."


def transaction_result(details: dict, states: dict[str, int]) -> dict:
    details["codes"] = sorted(set(details["codes"]))
    details["deletion_codes"] = list(dict.fromkeys(details["deletion_codes"]))
    problem = (
        sum(states[state] for state in UNSETTLED_TRANSACTION_STATES)
        + states["conflicted"]
        + details["quarantined_unresolved"]
        + details["overdue_writers"]
    )
    invalid_state = bool(details["state_invalid"])
    _append_state_deletion_codes(details, states)
    _append_live_deletion_codes(details)
    message = transaction_message(states, problem, invalid_state, details)
    status = _transaction_status(
        states, problem, invalid_state, details["quarantined_unresolved"]
    )
    return generation_maintenance.result("transactions", status, message, details)


def empty_transaction_details() -> tuple[dict, dict[str, int]]:
    states = {state: 0 for state in TRANSACTION_STATES}
    details: dict[str, Any] = {
        "states": states,
        "codes": [],
        "undo_retained": 0,
        "live_project_leases": 0,
        "live_writers": 0,
        "overdue_writers": 0,
        "live_maintenance_owners": 0,
        "quarantined_unresolved": 0,
        "read_error": False,
        "state_invalid": False,
        "deletion_codes": [],
    }
    return details, states


def _unusable_transaction_database(
    kind: str, path: Path, state_root: Path, details: dict, deadline: float
) -> dict | None:
    """The result for a database that cannot be scanned, or None to scan it."""
    if kind == "missing":
        return _missing_transaction_database(path, state_root, details, deadline)
    if kind != "regular":
        return _unreadable_transactions(details, "Transaction database is unsafe.")
    return None


def transaction_check(
    state_root: Path,
    now: datetime,
    deadline: float = float("inf"),
    *,
    vault_root: Path | None = None,
) -> dict:
    path = operational_database_path(state_root, "coordinator")
    details, states = empty_transaction_details()
    kind, _ = generation_maintenance.safe_kind(path, state_root)
    unusable = _unusable_transaction_database(
        kind, path, state_root, details, deadline
    )
    if unusable is not None:
        return unusable
    try:
        incomplete = scan_transaction_database(
            path, state_root, now, deadline, details, states, vault_root=vault_root
        )
    except (OSError, sqlite3.Error, TimeoutError, ValueError) as error:
        details["read_error_class"] = type(error).__name__
        return _unreadable_transactions(details, "Transaction state is unreadable.")
    if incomplete is not None:
        return incomplete
    return transaction_result(details, states)


QUEUE_COUNT_QUERIES = {
    "source_failures": "SELECT COUNT(*) FROM source_failures",
    "source_fences": "SELECT COUNT(*) FROM source_fences",
}
