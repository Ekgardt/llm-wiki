"""Whether a vault's reliability runtime is the complete adopted v3 layout."""
from __future__ import annotations

import contextlib
import os
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from install_state import validate_install_state
from llm_wiki import monotonic_deadline
from llm_wiki.adoption_admission import (
    COORDINATOR_CONTRACT,
    MAX_RECORD_BYTES,
    adoption_paths,
    identity_value,
    parse_timestamp,
)
from llm_wiki.adoption_admission import (
    kind as _kind,
)
from llm_wiki.formats.canonical import sha256_bytes
from llm_wiki.formats.queue_payload import validate_payload_blob
from llm_wiki.queue_v3_readers import audit_compile_progress, open_queue_v3_readonly
from llm_wiki.reliability_defaults import DEFAULTS
from llm_wiki.reliability_defaults import MAX_CAPTURE_INTENT_BYTES as MAX_CAPTURE_INTENT_BYTES
from llm_wiki.sqlite_deadlines import refresh_operational_deadline
from markdown_transaction import UNDO_RETENTION_DAYS as UNDO_RETENTION_DAYS
from reliable_memory import (
    OPERATIONAL_SCAN_BATCH_ROWS,
    fingerprint_runtime_file,
    open_readonly_operational_db,
    read_runtime_bytes,
    streamed_rows,
)

if TYPE_CHECKING:
    from operational_ownership import OwnerLease


# Runtime entry traversal keeps the same bound as doctor.MAX_RUNTIME_ENTRIES.
_MAX_RUNTIME_ENTRIES = 10_000


def _install_deletion_blockers(state_root: Path) -> list[str]:
    result = validate_install_state(state_root)
    codes = result.get("deletion_codes")
    if not isinstance(codes, list):
        return ["install_state_corrupt"]
    if any(not isinstance(code, str) for code in codes):
        return ["install_state_corrupt"]
    return codes


def _check_deadline(deadline: float) -> None:
    if monotonic_deadline.deadline_reached(deadline):
        raise TimeoutError("Reliability V3 validation deadline expired")


def _scanned_rows(
    database: sqlite3.Connection, query: str, *, deadline: float
) -> Iterator[sqlite3.Row]:
    """Every row, streamed within the deadline; a table that grows with use is never refused by count.

    A 10 000-row cap raised here and reported `transaction_state_unreadable`, which
    refuses a backup; the unpruned transactions are the two-day undo window, whose
    size is activity (6 834 on the busiest two days on record). See
    `docs/research/2026-09-27-doctor-reads-every-transaction.md`.
    """
    _check_deadline(deadline)
    return streamed_rows(database, query, stop=lambda: _check_deadline(deadline))


def _holds_a_row(database: sqlite3.Connection, table: str, *, deadline: float) -> bool:
    _check_deadline(deadline)
    return database.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone() is not None


def owner_matches(row: sqlite3.Row, owner: OwnerLease | None) -> bool:
    return owner is not None and (
        row["role"],
        row["scope"],
        row["actor_id"],
        row["owner_token"],
        row["fencing_epoch"],
        row["process_id"],
        row["process_start_identity"],
    ) == (
        owner.role,
        owner.scope,
        owner.actor_id,
        owner.token,
        owner.epoch,
        owner.process.pid,
        owner.process.start_identity,
    )


def _bounded_entries(
    directory: Path,
    *,
    state_root: Path,
    deadline: float,
) -> list[Path]:
    _check_deadline(deadline)
    kind = _kind(directory)
    if kind == "missing":
        return []
    if kind != "directory":
        raise ValueError("runtime artifact root is not a directory")
    return _scan_bounded_entries(directory, state_root=state_root, deadline=deadline)


def _scan_bounded_entries(
    directory: Path, *, state_root: Path, deadline: float
) -> list[Path]:
    entries: list[Path] = []
    with os.scandir(directory) as scanned:
        for entry in scanned:
            _append_bounded_entry(entries, entry.path, state_root=state_root, deadline=deadline)
    return entries


def _append_bounded_entry(
    entries: list[Path], value: str, *, state_root: Path, deadline: float
) -> None:
    _check_deadline(deadline)
    if len(entries) >= _MAX_RUNTIME_ENTRIES:
        raise ValueError("runtime artifact scan exceeded its bound")
    entries.append(_contained_runtime_entry(value, state_root))


def _contained_runtime_entry(value: str, state_root: Path) -> Path:
    path = Path(value)
    try:
        path.resolve(strict=True).relative_to(state_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise PermissionError("runtime artifact escaped the state root") from exc
    return path


_QUEUE_TABLE_CODES = {
    "attempt_history": "queue_attempt_history_retained",
    "source_fences": "queue_source_fence_retained",
    "source_failures": "queue_source_failure_retained",
    "task_source_links": "queue_source_link_retained",
    "capture_task_links": "capture_link_retained",
    "capture_task_link_resolutions": "capture_link_resolution_retained",
    "capture_task_link_seals": "capture_link_seal_retained",
    "semantic_decisions": "capture_semantic_decision_retained",
    "task_fences": "queue_task_fence_orphan",
    "task_purge_authorizations": "queue_purge_authorization_retained",
    "corrupt_export_operations": "queue_corrupt_export_retained",
    "corrupt_export_pages": "queue_corrupt_export_retained",
    "corrupt_dispositions": "queue_corrupt_disposition_retained",
    "corrupt_package_supersession_operations": "queue_corrupt_package_retained",
    "corrupt_package_supersession_pages": "queue_corrupt_package_retained",
    "corrupt_package_supersessions": "queue_corrupt_package_retained",
    "corrupt_purge_operations": "queue_corrupt_purge_retained",
    "corrupt_purge_pages": "queue_corrupt_purge_retained",
}


def validate_queue_v3_runtime(
    *,
    state_root: Path,
    now: datetime,
    deadline: float,
    excluded_owner: OwnerLease | None,
) -> list[str]:
    """Return bounded queue-v3 retention and consistency blockers."""
    del now, excluded_owner
    blockers: set[str] = set()
    try:
        blockers.update(queue_database_blockers(Path(state_root), deadline))
        blockers.update(_queue_artifact_blockers(Path(state_root), deadline))
    except TimeoutError:
        raise
    except (OSError, PermissionError, sqlite3.Error, ValueError):
        blockers.add("queue_state_unreadable")
    return sorted(blockers)


def queue_database_blockers(state_root: Path, deadline: float) -> set[str]:
    path = state_root / "run" / "queue-v3.sqlite3"
    with contextlib.closing(
        open_queue_v3_readonly(path, state_root, deadline=deadline)
    ) as database:
        blockers = _queue_task_blockers(database, deadline)
        blockers.update(_queue_table_blockers(database, deadline))
        blockers.update(capture_intent_blockers(database, state_root, deadline))
        blockers.update(_compile_progress_blockers(database, state_root, deadline))
        if _holds_a_row(database, "queue_ownership", deadline=deadline):
            blockers.add("queue_owner_projection_orphan")
    return blockers


def _compile_progress_blockers(
    database: sqlite3.Connection, state_root: Path, deadline: float
) -> set[str]:
    report = audit_compile_progress(database, state_root, deadline=deadline)
    return {"queue_compile_progress_retained"} if report["events"] else set()


def _queue_task_blockers(
    database: sqlite3.Connection, deadline: float
) -> set[str]:
    blockers: set[str] = set()
    for row in _scanned_rows(database, "SELECT * FROM tasks", deadline=deadline):
        blockers.add("queue_task_retained")
        _validate_queue_task(row)
    return blockers


def _validate_queue_task(row: sqlite3.Row) -> None:
    _require_task_id(row["id"])
    validation = validate_payload_blob(
        bytes(row["payload_blob"]), row["input_hash"], parse=True
    )
    if validation.code is None:
        return
    _require_expected_hash_failure(row)


def _require_task_id(value: object) -> None:
    if not isinstance(value, str):
        raise ValueError("queue task ID is invalid")
    if not value:
        raise ValueError("queue task ID is invalid")


def _require_expected_hash_failure(row: sqlite3.Row) -> None:
    if row["state"] != "dead":
        raise ValueError("queue task payload is invalid")
    if row["error_code"] != "payload_hash_mismatch":
        raise ValueError("queue task payload is invalid")


_COORDINATOR_TABLE_CODES = (
    ("project_leases", "project_lease_orphan"),
    ("writer_owners", "writer_owner_orphan"),
    ("intent_fences", "intent_fence_orphan"),
    ("capture_binding_projections", "capture_binding_projection_retained"),
)


_TRANSACTION_STATE_CODES = {
    "preparing": "transaction_nonterminal",
    "prepared": "transaction_nonterminal",
    "applying": "transaction_nonterminal",
    "aborting": "transaction_nonterminal",
    "conflicted": "transaction_conflicted",
    "quarantined": "transaction_quarantined",
}


def _queue_table_blockers(
    database: sqlite3.Connection, deadline: float
) -> set[str]:
    blockers: set[str] = set()
    for table, code in _QUEUE_TABLE_CODES.items():
        if _holds_a_row(database, table, deadline=deadline):
            blockers.add(code)
    return blockers


def capture_intent_blockers(
    database: sqlite3.Connection, state_root: Path, deadline: float, *, verified: set[str] | None = None,
) -> set[str]:
    blockers: set[str] = set()
    for row in capture_intent_rows(database, deadline):
        blockers.add("capture_intent_retained")
        validate_capture_intent(row, state_root, deadline=deadline)
        if verified is not None:
            verified.add(str(row["intent_id"]))
    return blockers


def capture_intent_rows(database: sqlite3.Connection, deadline: float) -> Iterator[sqlite3.Row]:
    """Finish each bounded SELECT before source I/O can delay a writer's commit."""
    after = ""
    while True:
        _check_deadline(deadline)
        # Full evidence scans can outlive the doctor's short initial read wait.
        # Reuse the queue's lock budget, clamped to this caller's remaining time.
        refresh_operational_deadline(database, DEFAULTS.queue_busy_ms, deadline)
        rows = database.execute(
            "SELECT * FROM capture_intents WHERE intent_id > ? ORDER BY intent_id LIMIT ?",
            (after, OPERATIONAL_SCAN_BATCH_ROWS),
        ).fetchall()
        if not rows:
            return
        after = str(rows[-1]["intent_id"])
        yield from rows


def validate_capture_intent(row: sqlite3.Row, state_root: Path, *, deadline: float) -> None:
    relative = row["relative_path"]
    if not _valid_capture_path(relative):
        raise ValueError("capture intent path is invalid")
    payload = read_runtime_bytes(
        state_root / relative,
        state_root,
        max_bytes=MAX_CAPTURE_INTENT_BYTES,
        owner_only=True,
    )
    _require_capture_payload(payload, row)
    require_capture_document(payload, row, state_root, deadline)


def require_capture_document(payload: bytes, row: sqlite3.Row, state_root: Path, deadline: float) -> None:
    from breadcrumb_protocol import RecordParsing, using_record_parsing
    from capture_handler_identity import (
        verified_capture_handler,
    )

    _check_deadline(deadline)
    try:
        with using_record_parsing(RecordParsing(active=partial(_check_deadline, deadline))):
            verified_capture_handler(state_root, dict(row), payload, deadline=deadline)
    except (KeyError, TypeError, RuntimeError) as error:
        raise ValueError("capture intent format or identity is invalid") from error
    _check_deadline(deadline)


def _require_capture_payload(payload: bytes, row: sqlite3.Row) -> None:
    if len(payload) != row["byte_size"]:
        raise ValueError("capture intent size changed")
    if sha256_bytes(payload) != row["intent_sha256"]:
        raise ValueError("capture intent digest changed")


def _valid_capture_path(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("run/capture-intents/"):
        return False
    if "\\" in value or ".." in Path(value).parts:
        return False
    return True


def _queue_artifact_blockers(state_root: Path, deadline: float) -> set[str]:
    blockers: set[str] = set()
    for relative, code in (
        ("run/capture-intents", "capture_intent_retained"),
        ("run/queue-results", "queue_result_retained"),
        ("run/queue-quarantine", "queue_quarantine_retained"),
    ):
        if _bounded_entries(
            state_root / relative, state_root=state_root, deadline=deadline
        ):
            blockers.add(code)
    return blockers


def validate_coordinator_v3_runtime(
    *,
    state_root: Path,
    now: datetime,
    deadline: float,
    excluded_owner: OwnerLease | None,
) -> list[str]:
    """Return bounded coordinator-v3 retention and consistency blockers."""
    blockers: set[str] = set()
    try:
        state = Path(state_root)
        database_blockers, known, retained = coordinator_database_blockers(
            state, now, deadline, excluded_owner
        )
        blockers.update(database_blockers)
        blockers.update(_transaction_artifact_blockers(state, deadline, known, retained))
    except TimeoutError:
        raise
    except (OSError, PermissionError, sqlite3.Error, ValueError):
        blockers.add("transaction_state_unreadable")
    return sorted(blockers)


def coordinator_database_blockers(
    state_root: Path,
    now: datetime,
    deadline: float,
    excluded_owner: OwnerLease | None,
) -> tuple[set[str], set[str], set[str]]:
    path = state_root / "run" / "markdown-transactions-v3.sqlite3"
    with contextlib.closing(
        open_readonly_operational_db(
            path,
            state_root,
            max_bytes=None,
            deadline=deadline,
            owner_only=True,
            contract=COORDINATOR_CONTRACT,
        )
    ) as database:
        blockers = _coordinator_owner_blockers(database, deadline, excluded_owner)
        blockers.update(_coordinator_table_blockers(database, deadline))
        transaction_blockers, known, retained = _transaction_blockers(
            database, state_root, now, deadline
        )
        blockers.update(transaction_blockers)
        known |= _recorded_transactions(database, _artifact_ids(state_root, deadline) - known)
    return blockers, known, retained


def _artifact_ids(state_root: Path, deadline: float) -> set[str]:
    return {_transaction_artifact_id(entry) for entry in _artifact_entries(state_root, deadline) if not _staged_prune(entry)}


def _artifact_entries(state_root: Path, deadline: float) -> list[Path]:
    return _bounded_entries(state_root / "run" / "transactions", state_root=state_root, deadline=deadline)


def _staged_prune(entry: Path) -> bool:
    """`.<id>.pruning-<uuid>`: images a prune set aside and did not finish removing.

    It read as an invalid artifact name, so an interrupted prune reported the whole
    transaction state unreadable (audit 2026-09-26 B-22). It is named instead, and
    the next prune or `doctor --repair` settles it.
    """
    return entry.name.startswith(".") and ".pruning-" in entry.name


def _recorded_transactions(database: sqlite3.Connection, ids: set[str]) -> set[str]:
    """Which of `ids` the ledger names, asked in slices under SQLite's variable limit."""
    ordered = sorted(ids)
    found: set[str] = set()
    for start in range(0, len(ordered), 500):
        chunk = ordered[start : start + 500]
        marks = ",".join("?" * len(chunk))
        rows = database.execute(f'SELECT id FROM "transaction" WHERE id IN ({marks})', chunk).fetchall()
        found.update(str(row[0]) for row in rows)
    return found


def _coordinator_owner_blockers(
    database: sqlite3.Connection,
    deadline: float,
    excluded_owner: OwnerLease | None,
) -> set[str]:
    blockers: set[str] = set()
    for row in _scanned_rows(database, "SELECT * FROM maintenance_owners", deadline=deadline):
        if not owner_matches(row, excluded_owner):
            blockers.add("canonical_owner_retained")
    return blockers


def _coordinator_table_blockers(
    database: sqlite3.Connection, deadline: float
) -> set[str]:
    blockers: set[str] = set()
    for table, code in _COORDINATOR_TABLE_CODES:
        if _holds_a_row(database, table, deadline=deadline):
            blockers.add(code)
    return blockers


def _transaction_blockers(
    database: sqlite3.Connection, state_root: Path, now: datetime, deadline: float
) -> tuple[set[str], set[str], set[str]]:
    # A committed row whose artifacts were pruned holds nothing: it produces no
    # blocker and no retention. Reading all of them hit the 10 000-row bound on
    # a vault with 23 664 rows and reported the state unreadable (audit
    # 2026-09-26 A-11, docs/research/2026-09-26-a-backup-takes-what-any-installed-vault-holds.md).
    rows = _scanned_rows(
        database,
        'SELECT * FROM "transaction" WHERE state <> \'committed\' OR artifacts_pruned_at IS NULL',
        deadline=deadline,
    )
    cutoff = now.astimezone(UTC) - timedelta(days=UNDO_RETENTION_DAYS)
    blockers: set[str] = set()
    known: set[str] = set()
    retained: set[str] = set()
    for row in rows:
        transaction_id, code, keep = _transaction_retention(row, state_root, cutoff)
        known.add(transaction_id)
        if code is not None:
            blockers.add(code)
        if keep:
            retained.add(transaction_id)
    return blockers, known, retained


def _transaction_retention(
    row: sqlite3.Row, state_root: Path, cutoff: datetime
) -> tuple[str, str | None, bool]:
    transaction_id = _transaction_id(row["id"])
    state = row["state"]
    code = _TRANSACTION_STATE_CODES.get(state)
    if code is not None:
        return transaction_id, code, True
    return _settled_transaction_retention(row, state_root, cutoff, transaction_id, state)


def _settled_transaction_retention(
    row: sqlite3.Row,
    state_root: Path,
    cutoff: datetime,
    transaction_id: str,
    state: object,
) -> tuple[str, str | None, bool]:
    if state in {"committed", "aborted"}:
        return _terminal_transaction_retention(row, state_root, cutoff, transaction_id)
    if state == "discarded":
        return transaction_id, None, False
    raise ValueError("transaction state is unknown")


def _transaction_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("transaction ID is invalid")
    if not value:
        raise ValueError("transaction ID is invalid")
    return value


def _terminal_transaction_retention(
    row: sqlite3.Row, state_root: Path, cutoff: datetime, transaction_id: str
) -> tuple[str, str | None, bool]:
    if row["state"] == "aborted":
        _validate_abort_receipt(row, state_root, transaction_id)
    updated = parse_timestamp(row["updated_at"])
    if updated >= cutoff and row["artifacts_pruned_at"] is None:
        return transaction_id, "transaction_undo_retained", True
    return transaction_id, None, False


def _validate_abort_receipt(
    row: sqlite3.Row, state_root: Path, transaction_id: str
) -> None:
    receipt = state_root / "run" / "transactions" / transaction_id / "abort-receipt.json"
    payload = read_runtime_bytes(
        receipt, state_root, max_bytes=MAX_RECORD_BYTES, owner_only=True
    )
    if sha256_bytes(payload) != row["abort_receipt_sha256"]:
        raise ValueError("aborted transaction receipt changed")


def _transaction_artifact_blockers(
    state_root: Path,
    deadline: float,
    known: set[str],
    retained: set[str],
) -> set[str]:
    artifact_ids = _artifact_ids(state_root, deadline)
    staged = any(_staged_prune(entry) for entry in _artifact_entries(state_root, deadline))
    findings = (
        (staged, "transaction_prune_interrupted"),
        (bool(retained & artifact_ids), "transaction_artifact_retained"),
        (bool(artifact_ids - known), "transaction_artifact_state_unknown"),
    )
    return {code for present, code in findings if present}


def _transaction_artifact_id(entry: Path) -> str:
    if _kind(entry) != "directory":
        raise ValueError("transaction artifact is not a directory")
    if re.fullmatch(r"[0-9a-z_-]{1,128}", entry.name) is None:
        raise ValueError("transaction artifact name is invalid")
    return entry.name


def blackboard_claim_blockers(
    *, state_root: Path, now: datetime, deadline: float
) -> list[str]:
    path = Path(state_root) / "run" / "markdown-transactions-v3.sqlite3"
    blockers: set[str] = set()
    try:
        if _blackboard_claim_live(path, state_root=state_root, now=now, deadline=deadline):
            blockers.add("blackboard_claim_retained")
    except TimeoutError:
        raise
    except (OSError, PermissionError, sqlite3.Error, ValueError):
        blockers.add("transaction_state_unreadable")
    return sorted(blockers)


def _blackboard_claim_live(
    path: Path, *, state_root: Path, now: datetime, deadline: float
) -> bool:
    with contextlib.closing(
        open_readonly_operational_db(
            path,
            state_root,
            max_bytes=None,
            deadline=deadline,
            owner_only=True,
            contract=COORDINATOR_CONTRACT,
        )
    ) as database:
        rows = _scanned_rows(
            database,
            "SELECT expires_at FROM blackboard_claims",
            deadline=deadline,
        )
        return any(parse_timestamp(row["expires_at"]) > now for row in rows)


def validate_reliability_v3_runtime(
    *,
    root: Path,
    state_root: Path,
    now: datetime,
    deadline: float,
    excluded_owner: OwnerLease | None,
) -> list[str]:
    """Validate adopted operational state without mutating or acquiring ownership."""
    del root
    blockers = set(
        validate_queue_v3_runtime(
            state_root=state_root,
            now=now,
            deadline=deadline,
            excluded_owner=excluded_owner,
        )
    )
    blockers.update(
        validate_coordinator_v3_runtime(
            state_root=state_root,
            now=now,
            deadline=deadline,
            excluded_owner=excluded_owner,
        )
    )
    blockers.update(
        blackboard_claim_blockers(
            state_root=state_root,
            now=now,
            deadline=deadline,
        )
    )
    blockers.update(_install_deletion_blockers(Path(state_root)))
    paths = adoption_paths(Path(state_root))
    if any(_kind(paths[name]) != "missing" for name in ("queue_retired", "coordinator_retired")):
        blockers.add("retired_operational_database_retained")
    for path, code in (
        (Path(state_root) / "run" / "compile.pid", "compile_marker_retained"),
        (Path(state_root) / "run" / "maintenance.lock", "maintenance_marker_retained"),
    ):
        if _kind(path) != "missing":
            blockers.add(code)
    _check_deadline(deadline)
    return sorted(blockers)


def artifact_record(
    path: Path, state_root: Path, max_bytes: int | None, *, deadline=None,
) -> dict[str, object]:
    digest, identity = fingerprint_runtime_file(
        path, state_root, max_bytes=max_bytes, deadline=deadline,
    )
    return {
        "path": path.relative_to(state_root).as_posix(),
        "sha256": digest,
        "identity": identity_value(identity),
    }
