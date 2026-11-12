"""Doctor's checks of the adopted queue, its owners and its dead work."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

import generation_maintenance  # noqa: E402
from llm_wiki import doctor_base, monotonic_deadline
from llm_wiki.doctor_base import (
    MAX_MANIFEST_BYTES,
    MAX_OPERATIONAL_ROWS,
    QUEUE_STATES,
    archive_path,
    database_sidecar_present,
    database_tables,
    owner_row_known,
    read_bounded_json,
)
from llm_wiki.doctor_transactions import QUEUE_COUNT_QUERIES, operational_database_path
from reliable_memory import (
    read_runtime_bytes,
)

# A stray pre-adoption candidate is moved into a quarantine directory, never
# deleted; both directories are retained evidence for the `run/` deletion
# contract. The rule lives in `installed_memory_repair.retire_stray_candidates`.
# See `docs/research/2026-09-24-a-stray-candidate-is-retired-where-it-refuses.md`.
COORDINATOR_QUARANTINE = "run/coordinator-quarantine"


# Entries read from a pre-v4.0.0 `run/queue/` directory, which doctor only names and
# refuses; past it the scan is reported truncated (`artifact_truncated`). Basis
# unknown: value predates measurement; review never — the directory is retired.
MAX_QUEUE_FILES = 200


# One queue result doctor reads back to check it. The largest result on the installed
# vault on 2026-09-27 was 12 143 bytes, so 8 MiB is a guard on an untrusted file,
# about 690 times a real one; refused past it, never cut.
MAX_QUEUE_RESULT_BYTES = 8 * 1024 * 1024


# Entries one runtime directory listing takes; past it the scan is reported truncated
# and deletion stays refused (`archive_scan_truncated`, `artifact_truncated`). Basis
# unknown: value predates measurement; `run/transactions/` held 5 547 entries on
# 2026-09-27 — review when a listing nears it.
MAX_RUNTIME_ENTRIES = 10_000


def _queue_artifact_state(state_root: Path, deadline: float) -> dict[str, Any]:
    details: dict[str, Any] = {
        "legacy_retained": 0,
        "results_retained": 0,
        "queue_quarantined": 0,
        "coordinator_quarantined": 0,
        "artifact_error": False,
        "artifact_truncated": False,
        "deletion_codes": [],
    }
    legacy = state_root / "run" / "queue"
    entries, truncated, error = _bounded_runtime_entries(
        legacy,
        state_root,
        limit=MAX_QUEUE_FILES,
        deadline=deadline,
    )
    details["artifact_truncated"] |= truncated
    details["artifact_error"] |= error
    _count_legacy_queue_entries(entries, state_root, details)
    for key, relative in (
        ("results_retained", "run/queue-results"),
        ("queue_quarantined", "run/queue-quarantine"),
        ("coordinator_quarantined", COORDINATOR_QUARANTINE),
    ):
        _count_queue_artifact_directory(
            state_root, relative, key, deadline, details
        )
    _append_queue_artifact_codes(details)
    return details


def _count_legacy_queue_entries(
    entries: list[Path], state_root: Path, details: dict
) -> None:
    """Entries of the JSON queue of v3.3.0–v3.4.0: counted and kept, never read.

    No release imports them since 2026-09-23; each one keeps `run/` from deletion,
    and one that is not a regular file leaves the state unknown.
    """
    details["legacy_retained"] += len(entries)
    if _any_irregular_entry(entries, state_root):
        details["artifact_error"] = True


def _count_queue_artifact_directory(
    state_root: Path, relative: str, key: str, deadline: float, details: dict
) -> None:
    count, truncated, error = _runtime_artifact_count(
        state_root / relative, state_root, deadline
    )
    details[key] = count
    details["artifact_truncated"] |= truncated
    details["artifact_error"] |= error


def _runtime_artifact_count(
    directory: Path, root: Path, deadline: float
) -> tuple[int, bool, bool]:
    kind, _ = generation_maintenance.safe_kind(directory, root)
    if kind == "missing":
        return 0, False, False
    if kind != "directory":
        return 0, False, True
    return _scanned_artifact_count(directory, root, deadline)


@dataclass
class _ArtifactTally:
    count: int = 0
    error: bool = False


def _tally_artifacts(
    scanned: Iterator[os.DirEntry], root: Path, deadline: float, tally: _ArtifactTally
) -> bool:
    for entry in scanned:
        if monotonic_deadline.deadline_reached(deadline):
            return True
        tally.count += 1
        tally.error = tally.error or generation_maintenance.safe_kind(Path(entry.path), root)[0] != "regular"
    return False


def _scanned_artifact_count(
    directory: Path, root: Path, deadline: float
) -> tuple[int, bool, bool]:
    tally = _ArtifactTally()
    try:
        with os.scandir(directory) as scanned:
            truncated = _tally_artifacts(scanned, root, deadline, tally)
    except OSError:
        return tally.count, False, True
    return tally.count, truncated, tally.error


def _append_queue_artifact_codes(details: dict) -> None:
    for key, code in (
        ("legacy_retained", "legacy_queue_retained"),
        ("results_retained", "queue_result_retained"),
        ("queue_quarantined", "queue_quarantine_retained"),
        ("coordinator_quarantined", "coordinator_quarantine_retained"),
    ):
        if details[key]:
            details["deletion_codes"].append(code)
    if details["artifact_error"] or details["artifact_truncated"]:
        details["deletion_codes"].append("queue_artifact_state_unknown")


def _unreadable_queue_result(state_root: Path, deadline: float, message: str) -> dict:
    details = _queue_artifact_state(state_root, deadline)
    details.update(read_error=True, states={state: 0 for state in QUEUE_STATES})
    details["deletion_codes"].append("queue_state_unreadable")
    return generation_maintenance.result("queue", "error", message, details)


def _adjusted_queue_status(status: str, artifacts: dict) -> str:
    if not artifacts["deletion_codes"]:
        return status
    if artifacts["artifact_error"]:
        return "error"
    return "degraded" if status == "ok" else status


def _json_queue_status(retained: int) -> tuple[str, str]:
    if retained:
        return "degraded", (
            f"run/queue holds {retained} record(s) of the retired JSON queue; "
            "this release does not import them."
        )
    return "ok", "Queue has no database yet and no retained work."


def _legacy_queue_result(state_root: Path, deadline: float) -> dict:
    """The queue check of a vault that has no queue database.

    The JSON queue of v3.3.0–v3.4.0 is not imported since 2026-09-23: entries
    under `run/queue/` are counted and named, never read.
    """
    artifacts = _queue_artifact_state(state_root, deadline)
    details = dict(artifacts, read_error=False)
    status, message = _json_queue_status(artifacts["legacy_retained"])
    return generation_maintenance.result("queue", _adjusted_queue_status(status, artifacts), message, details)


def _unreadable_queue_reason(
    database_kind: str, database_path: Path, state_root: Path
) -> str | None:
    """Why an absent queue database still cannot be read as absent."""
    if database_kind != "missing":
        return "Queue database is unsafe."
    if database_sidecar_present(database_path, state_root):
        return "Queue sidecars lack a database."
    return None


def queue_check(state_root: Path, now: datetime, deadline: float) -> dict:
    database_path = operational_database_path(state_root, "queue")
    database_kind = generation_maintenance.safe_kind(database_path, state_root)[0]
    if database_kind == "regular":
        return queue_v2_check(state_root, now, deadline)
    unreadable = _unreadable_queue_reason(database_kind, database_path, state_root)
    if unreadable is not None:
        return _unreadable_queue_result(state_root, deadline, unreadable)
    return _legacy_queue_result(state_root, deadline)


def _bounded_runtime_entries(
    directory: Path,
    root: Path,
    *,
    limit: int,
    deadline: float,
) -> tuple[list[Path], bool, bool]:
    kind, _ = generation_maintenance.safe_kind(directory, root)
    if kind == "missing":
        return [], False, False
    if kind != "directory":
        return [], False, True
    return _scanned_entries(directory, limit, deadline)


def _entry_budget_spent(entries: list[Path], limit: int, deadline: float) -> bool:
    return monotonic_deadline.deadline_reached(deadline) or len(entries) >= limit


def _scanned_entries(
    directory: Path, limit: int, deadline: float
) -> tuple[list[Path], bool, bool]:
    entries: list[Path] = []
    try:
        with os.scandir(directory) as scanned:
            truncated = _collect_entries(scanned, entries, limit, deadline)
    except OSError:
        return entries, False, True
    return entries, truncated, False


def _collect_entries(
    scanned: Iterator[os.DirEntry], entries: list[Path], limit: int, deadline: float
) -> bool:
    for entry in scanned:
        if _entry_budget_spent(entries, limit, deadline):
            return True
        entries.append(Path(entry.path))
    return False


class _TaskVerdict(NamedTuple):
    unknown_state: bool
    corrupt: bool


class _QueueScan(NamedTuple):
    result: dict | None
    unknown_state: bool
    corrupt_metadata: bool


class _QueueTaskScan(NamedTuple):
    unknown_state: bool
    corrupt_metadata: bool
    references: set[str]
    result_hashes: dict[str, object]
    retained: bool
    dead_unresolved: int
    oldest_dead_days: int | None


def _empty_queue_details() -> tuple[dict, dict[str, int]]:
    states = {state: 0 for state in QUEUE_STATES}
    details: dict[str, Any] = {
        "states": states,
        "codes": [],
        "capabilities": [],
        "live_workers": 0,
        "live_migrations": 0,
        "results_retained": 0,
        "results_invalid": 0,
        "source_failures": 0,
        "source_fences": 0,
        "migration": "not-started",
        "read_error": False,
        "deletion_codes": [],
    }
    return details, states


def _record_queue_migration(state_root: Path, details: dict) -> None:
    from markdown_transaction import reliability_v3_records_present

    # Adoption retired the v2 queue and left a tombstone where it stood; before
    # adoption there is nothing to migrate either, since 2026-09-23 the JSON
    # queue import went and the `queue-migrated-v2` marker is read by nothing.
    adopted = reliability_v3_records_present(state_root)
    details["migration"] = "retired" if adopted else "none"


def _valid_queue_error_code(error_code: object) -> bool:
    if error_code is None:
        return True
    if not isinstance(error_code, str) or not 1 <= len(error_code) <= 200:
        return False
    return not any(char in error_code for char in "\r\n")


def _failed_state_metadata(
    state: str, error_code: object, blocked_capability: object
) -> bool:
    if error_code is None:
        return False
    if state == "blocked":
        return isinstance(blocked_capability, str) and bool(blocked_capability)
    return state == "dead" and blocked_capability is None


def _queue_error_metadata_valid(
    state: str,
    error_code: object,
    blocked_capability: object,
    valid_error_code: bool,
) -> bool:
    if not valid_error_code:
        return False
    if state == "ready":
        return blocked_capability is None
    return _failed_state_metadata(state, error_code, blocked_capability)


def _queue_metadata_matches_state(
    state: str,
    error_code: object,
    blocked_capability: object,
    valid_error_code: bool,
) -> bool:
    """Each task state allows exactly one shape of error metadata."""
    if state in {"leased", "succeeded"}:
        return error_code is None and blocked_capability is None
    if state == "cancelled":
        return error_code == "cancelled" and blocked_capability is None
    return _queue_error_metadata_valid(
        state, error_code, blocked_capability, valid_error_code
    )


def _collect_task_codes(
    error_code: object,
    blocked_capability: object,
    codes: set[str],
    capabilities: set[str],
) -> None:
    if error_code:
        codes.add(str(error_code))
    if blocked_capability:
        capabilities.add(str(blocked_capability))


def _collect_task_result(
    row: sqlite3.Row,
    row_columns: set[str],
    references: set[str],
    result_hashes: dict[str, object],
) -> None:
    if "result_reference" not in row_columns or not row["result_reference"]:
        return
    reference = str(row["result_reference"])
    references.add(reference)
    stored = row["result_sha256"] if "result_sha256" in row_columns else None
    result_hashes[reference] = stored


def _task_lease_live(
    row: sqlite3.Row, row_columns: set[str], state: str, now: datetime
) -> bool:
    if state != "leased" or "lease_expires_at" not in row_columns:
        return False
    expires = generation_maintenance.parse_utc(row["lease_expires_at"]) or datetime.min.replace(
        tzinfo=UTC
    )
    return expires > now


def _scan_one_task_row(
    row: sqlite3.Row,
    *,
    now: datetime,
    states: dict[str, int],
    details: dict,
    codes: set[str],
    capabilities: set[str],
    references: set[str],
    result_hashes: dict[str, object],
) -> _TaskVerdict:
    row_columns = set(row.keys())
    state = row["state"]
    if not isinstance(state, str) or state not in QUEUE_STATES:
        return _TaskVerdict(True, False)
    states[state] += 1
    if not {"error_code", "blocked_capability"}.issubset(row_columns):
        return _TaskVerdict(False, True)
    error_code = row["error_code"]
    blocked_capability = row["blocked_capability"]
    matches = _queue_metadata_matches_state(
        state, error_code, blocked_capability, _valid_queue_error_code(error_code)
    )
    _collect_task_codes(error_code, blocked_capability, codes, capabilities)
    _collect_task_result(row, row_columns, references, result_hashes)
    details["live_workers"] += int(_task_lease_live(row, row_columns, state, now))
    return _TaskVerdict(False, not matches)


def _scan_queue_tasks(
    rows: Iterator[sqlite3.Row],
    *,
    now: datetime,
    deadline: float,
    details: dict,
    states: dict[str, int],
) -> _QueueTaskScan:
    codes: set[str] = set()
    capabilities: set[str] = set()
    references: set[str] = set()
    result_hashes: dict[str, object] = {}
    unknown_state = False
    corrupt_metadata = False
    retained = False
    backlog: list[dict[str, object]] = []
    for row in rows:
        _queue_scan_stop(deadline)
        retained = True
        _remember_task_backlog(row, backlog)
        verdict = _scan_one_task_row(
            row,
            now=now,
            states=states,
            details=details,
            codes=codes,
            capabilities=capabilities,
            references=references,
            result_hashes=result_hashes,
        )
        unknown_state = unknown_state or verdict.unknown_state
        corrupt_metadata = corrupt_metadata or verdict.corrupt
    details["codes"] = sorted(set(details["codes"]) | codes)
    details["capabilities"] = sorted(capabilities)
    dead, oldest = dead_backlog(backlog, now)
    return _QueueTaskScan(
        unknown_state, corrupt_metadata, references, result_hashes,
        retained, dead, oldest,
    )


def _queue_scan_stop(deadline: float) -> None:
    if monotonic_deadline.deadline_reached(deadline):
        raise TimeoutError("queue check deadline")


def _remember_task_backlog(
    row: sqlite3.Row, backlog: list[dict[str, object]]
) -> None:
    if row["state"] == "dead" or _has_answering_redrive(row):
        backlog.append(_task_backlog_projection(row))


def _has_answering_redrive(row: sqlite3.Row) -> bool:
    if "redrive_of" not in row.keys():
        return False
    return _answers_its_origin(row)


def _task_backlog_projection(row: sqlite3.Row) -> dict[str, object]:
    return {
        key: row[key] for key in ("id", "state", "updated_at", "redrive_of")
        if key in row.keys()
    }


def _queue_task_rows(database: sqlite3.Connection) -> Iterator[sqlite3.Row]:
    return iter(database.execute(
        "SELECT * FROM tasks ORDER BY state IN ('succeeded', 'dead', 'cancelled'), rowid DESC"
    ))


def _append_queue_scan_codes(
    details: dict, unknown_state: bool, corrupt_metadata: bool, retained: bool
) -> None:
    ordered = (
        (unknown_state, "queue_state_unknown"),
        (corrupt_metadata, "queue_state_corrupt"),
        (retained, "queue_task_retained"),
    )
    for flagged, code in ordered:
        if flagged:
            details["deletion_codes"].append(code)


def _count_queue_rows(
    database: sqlite3.Connection,
    tables: set[str],
    details: dict,
    *,
    table: str,
    retained_code: str,
) -> None:
    if table not in tables:
        return
    count = database.execute(QUEUE_COUNT_QUERIES[table]).fetchone()[0]
    details[table] = count
    if count:
        details["deletion_codes"].append(retained_code)


class _QueueOwnerColumns(NamedTuple):
    token: str
    pid: str
    role: str


# The legacy queue and the adopted v3 queue name the same owner facts differently;
# reading the v3 row by the legacy names raised IndexError and doctor printed no
# report whenever a v3 worker held the queue (2026-09-29, the installer's smoke).
_LEGACY_QUEUE_OWNER = _QueueOwnerColumns("token", "pid", "role")
_V3_QUEUE_OWNER = _QueueOwnerColumns("owner_token", "process_id", "domain_role")


def _queue_owner_columns(row: sqlite3.Row) -> _QueueOwnerColumns:
    if _V3_QUEUE_OWNER.token in row.keys():
        return _V3_QUEUE_OWNER
    return _LEGACY_QUEUE_OWNER


def _count_owner_role(role: object, details: dict) -> None:
    if role == "worker":
        details["live_workers"] += 1
    if role == "migration":
        details["live_migrations"] += 1


def _count_live_owner_role(
    row: sqlite3.Row, columns: _QueueOwnerColumns, details: dict, now: datetime
) -> None:
    """Count the role of an owner row, when its lease is still live."""
    if not generation_maintenance.live_owner(row, now, pid_column=columns.pid):
        return
    _count_owner_role(row[columns.role], details)


def _count_one_queue_owner(row: sqlite3.Row, details: dict, now: datetime) -> None:
    columns = _queue_owner_columns(row)
    if row[columns.token] is None:
        return
    if not owner_row_known(row, pid_column=columns.pid):
        details["deletion_codes"].append("queue_owner_state_unknown")
        return
    _count_live_owner_role(row, columns, details, now)


def _count_queue_ownership(
    database: sqlite3.Connection, tables: set[str], details: dict, now: datetime,
    deadline: float,
) -> None:
    if "queue_ownership" not in tables:
        return
    for row in database.execute("SELECT * FROM queue_ownership"):
        _queue_scan_stop(deadline)
        _count_one_queue_owner(row, details, now)


def _count_queue_side_tables(
    database: sqlite3.Connection, tables: set[str], details: dict, now: datetime,
    deadline: float,
) -> None:
    _count_queue_rows(
        database,
        tables,
        details,
        table="source_failures",
        retained_code="queue_source_failure_retained",
    )
    _count_queue_rows(
        database,
        tables,
        details,
        table="source_fences",
        retained_code="queue_source_fence_retained",
    )
    _count_queue_ownership(database, tables, details, now, deadline)


def _queue_result_bytes(state_root: Path, reference: str) -> bytes | None:
    """The stored result, or None when the reference is unsafe or unreadable."""
    results = state_root / "run" / "queue-results"
    try:
        reference_path = Path(reference)
        if reference_path.is_absolute() or ".." in reference_path.parts:
            raise PermissionError("unsafe queue result reference")
        candidate = state_root / reference_path
        if candidate.parent.resolve(strict=True) != results.resolve(strict=True):
            raise PermissionError("queue result reference escapes result root")
        return read_runtime_bytes(
            candidate,
            state_root,
            max_bytes=MAX_QUEUE_RESULT_BYTES,
            owner_only=True,
        )
    except (OSError, PermissionError, ValueError):
        return None


def _validate_queue_results(
    state_root: Path,
    references: set[str],
    result_hashes: dict[str, object],
    details: dict,
    deadline: float,
) -> None:
    for reference in references:
        _queue_scan_stop(deadline)
        raw = _queue_result_bytes(state_root, reference)
        expected = result_hashes.get(reference)
        if raw is None or not isinstance(expected, str):
            details["results_invalid"] += 1
            continue
        if hashlib.sha256(raw).hexdigest() != expected:
            details["results_invalid"] += 1


def _queue_task_columns(
    database: sqlite3.Connection, tables: set[str], deadline: float
) -> set[str]:
    """The task columns, refusing a database that has no tasks table."""
    if "tasks" not in tables:
        raise sqlite3.DatabaseError("tasks table missing")
    return generation_maintenance.database_table_columns(database, "tasks", deadline)


def _scan_queue_database(
    path: Path,
    state_root: Path,
    now: datetime,
    deadline: float,
    details: dict,
    states: dict[str, int],
) -> _QueueScan:
    with doctor_base.readonly_database(path, state_root, deadline=deadline) as database:
        if monotonic_deadline.deadline_reached(deadline):
            raise TimeoutError("queue check deadline")
        tables = database_tables(database, deadline)
        task_columns = _queue_task_columns(database, tables, deadline)
        if not {"state", "error_code", "blocked_capability"}.issubset(task_columns):
            details["codes"].append("queue_metadata_missing")
            details["deletion_codes"].append("queue_state_corrupt")
            return _QueueScan(
                generation_maintenance.result("queue", "error", "Queue task metadata is incomplete.", details),
                False,
                False,
            )
        tasks = _scan_queue_tasks(
            _queue_task_rows(database), now=now, deadline=deadline,
            details=details, states=states
        )
        _append_queue_scan_codes(
            details, tasks.unknown_state, tasks.corrupt_metadata, tasks.retained
        )
        details["dead_unresolved"] = tasks.dead_unresolved
        details["oldest_dead_days"] = tasks.oldest_dead_days
        _count_queue_side_tables(database, tables, details, now, deadline)
        _validate_queue_results(
            state_root, tasks.references, tasks.result_hashes, details, deadline
        )
        _validate_capture_sources(database, tables, state_root, deadline, details)
        _validate_compile_progress(database, state_root, deadline, details)
        return _QueueScan(None, tasks.unknown_state, tasks.corrupt_metadata)


def _validate_compile_progress(database, state_root, deadline, details) -> None:
    from llm_wiki.queue_v3_readers import audit_compile_progress

    try:
        counts = audit_compile_progress(database, state_root, deadline=deadline)
    except (ValueError, sqlite3.Error) as error:
        details["compile_progress_error"] = str(error)
        raise ValueError("compile progress could not be verified") from error
    details["compile_progress"] = counts
    if counts["events"]:
        details["deletion_codes"].append("queue_compile_progress_retained")


def _validate_capture_sources(database, tables, state_root, deadline, details) -> None:
    from breadcrumb_storage import inspect_pending_sources
    from installed_memory_repair import (
        describe_failure,
    )
    from reliability_v3_validation import (
        capture_intent_blockers,
    )

    if "capture_intents" not in tables:
        return
    verified: set[str] = set()
    try:
        codes = capture_intent_blockers(database, state_root, deadline, verified=verified)
        pending = inspect_pending_sources(state_root, verified, deadline=deadline)
    except (OSError, sqlite3.Error, ValueError, KeyError, RuntimeError) as error:
        details["capture_intent_error"] = describe_failure(error)
        raise ValueError("capture evidence could not be verified") from error
    details["deletion_codes"].extend(sorted(codes))
    _record_pending_capture_health(pending, details)


def _record_pending_capture_health(pending: dict, details: dict) -> None:
    details["capture_pending_complete"] = pending["complete"]
    details["capture_incomplete"] = pending["incomplete"]
    if any(pending.values()):
        details["deletion_codes"].append("capture_intent_retained")


def _queue_error_state(
    details: dict, unknown_state: bool, corrupt_metadata: bool
) -> bool:
    if unknown_state or corrupt_metadata:
        return True
    return bool(details["results_invalid"])


def _queue_pending_work(states: dict[str, int], details: dict) -> bool:
    counts = [states[key] for key in ("ready", "leased", "blocked")]
    counts.extend(details.get(key, 0) for key in ("dead_unresolved", "capture_pending_complete", "capture_incomplete"))
    return any(counts)


# A dead task is work that did not happen, whatever its age, until it is redriven
# or the weekly purge exports it past the retention window. Counting only this
# week's reported 25 unresolved captures from 08-27..09-08 as healthy while the
# weekly that would export them had not run since 09-13 (audit 2026-09-27 B-13,
# docs/research/2026-09-27-a-dead-task-counts-until-it-is-resolved.md). The age
# is shown, not used to hide.
#
# A dead task with a redrive that was not cancelled is resolved by that redrive: it
# succeeded, it is queued again (and counted as ready), or it died and is counted
# itself. Counting the parent too named 25 captures on 2026-09-28 whose redrives had
# all been answered (docs/research/2026-09-28-a-check-names-its-cause.md).
def dead_backlog(
    rows: Sequence[Mapping[str, object] | sqlite3.Row], now: datetime
) -> tuple[int, int | None]:
    """(dead tasks no live or finished redrive answers, age in days of the oldest)."""
    answered = _redriven_ids(rows)
    moments = [_dead_moment(row) for row in rows if _unanswered_dead(row, answered)]
    return len(moments), _oldest_age_days(_known(moments), now)


def _known(moments: list[datetime | None]) -> list[datetime]:
    return [moment for moment in moments if moment is not None]


def _unanswered_dead(row: Mapping[str, object] | sqlite3.Row, answered: set[str]) -> bool:
    return row["state"] == "dead" and row["id"] not in answered


def _redriven_ids(rows: Sequence[Mapping[str, object] | sqlite3.Row]) -> set[str]:
    """Tasks that a redrive other than a cancelled one names as its origin."""
    if not rows or "redrive_of" not in rows[0].keys():
        return set()
    return {row["redrive_of"] for row in rows if _answers_its_origin(row)}


def _answers_its_origin(row: Mapping[str, object] | sqlite3.Row) -> bool:
    return bool(row["redrive_of"]) and row["state"] != "cancelled"


def _oldest_age_days(moments: list[datetime], now: datetime) -> int | None:
    if not moments:
        return None
    return (now - min(moments)).days


def _dead_moment(row: Mapping[str, object] | sqlite3.Row) -> datetime | None:
    if "updated_at" not in row.keys():
        return None
    return generation_maintenance.parse_utc(row["updated_at"])


def _queue_status(
    states: dict[str, int],
    details: dict,
    unknown_state: bool,
    corrupt_metadata: bool,
) -> str:
    if _queue_error_state(details, unknown_state, corrupt_metadata):
        return "error"
    if _queue_pending_work(states, details):
        return "degraded"
    return "ok"


def _append_queue_deletion_codes(details: dict) -> None:
    for key, code in (
        ("live_workers", "queue_worker_live"),
        ("live_migrations", "queue_migration_live"),
        ("results_invalid", "queue_result_state_unknown"),
    ):
        if details[key]:
            details["deletion_codes"].append(code)


def queue_v2_check(state_root: Path, now: datetime, deadline: float) -> dict:
    path = operational_database_path(state_root, "queue")
    details, states = _empty_queue_details()
    details.update(_queue_artifact_state(state_root, deadline))
    _record_queue_migration(state_root, details)
    try:
        scan = _scan_queue_database(path, state_root, now, deadline, details, states)
    except (OSError, PermissionError, sqlite3.Error, TimeoutError, ValueError):
        details["read_error"] = True
        details["deletion_codes"].append("queue_state_unreadable")
        return generation_maintenance.result("queue", "error", _queue_read_error_message(details), details)
    if scan.result is not None:
        return scan.result
    if time.monotonic() >= deadline:
        details["budget_exhausted"] = True
    status = _queue_status(states, details, scan.unknown_state, scan.corrupt_metadata)
    _append_queue_deletion_codes(details)
    return generation_maintenance.result("queue", status, queue_message(status, details), details)


def _queue_read_error_message(details: dict) -> str:
    compile_error = details.get("compile_progress_error")
    if compile_error:
        return f"Compile evidence cannot be verified: {compile_error}"
    capture_error = details.get("capture_intent_error")
    if capture_error:
        return f"Capture evidence cannot be verified: {capture_error}"
    return "Queue state is unreadable."


def queue_message(status: str, details: dict) -> str:
    """What needs attention, by name: a vague line left the owner nothing to act on."""
    if status == "ok":
        return "Queue state is healthy."
    return _capture_pending_message(details) or _queue_backlog_message(details)


def _capture_pending_message(details: dict) -> str | None:
    complete = details.get("capture_pending_complete", 0)
    incomplete = details.get("capture_incomplete", 0)
    if complete or incomplete:
        return f"Capture publication: {complete} complete pending record(s), {incomplete} incomplete record(s); retained for recovery."
    return None


def _queue_backlog_message(details: dict) -> str:
    dead = details.get("dead_unresolved")
    if not dead:
        return "Queue state requires operator attention."
    return (
        f"{dead} dead queue task(s) no redrive has answered, the oldest "
        f"{details.get('oldest_dead_days')} day(s) old."
    )


def _empty_archive_details() -> dict:
    return {
        "bags": 0,
        "duplicates": 0,
        "quarantined": 0,
        "index": "missing",
        "codes": [],
        "read_error": False,
        "deletion_codes": [],
    }


def _any_irregular_entry(entries: list[Path], state_root: Path) -> bool:
    return any(generation_maintenance.safe_kind(item, state_root)[0] != "regular" for item in entries)


def _record_quarantine_state(
    state_root: Path, deadline: float, details: dict
) -> None:
    quarantine = state_root / "run" / "archive-quarantine"
    entries, truncated, error = _bounded_runtime_entries(
        quarantine,
        state_root,
        limit=MAX_RUNTIME_ENTRIES,
        deadline=deadline,
    )
    details["quarantined"] = len(entries)
    if details["quarantined"]:
        details["deletion_codes"].append("archive_quarantine_retained")
    if truncated or error or _any_irregular_entry(entries, state_root):
        details["read_error"] = True
        details["deletion_codes"].append("archive_quarantine_state_unknown")


def _archive_month_directory(month: Path, root: Path) -> bool:
    if generation_maintenance.safe_kind(month, root)[0] != "directory":
        return False
    return re.fullmatch(r"\d{4}-\d{2}", month.name) is not None


def _archive_months(
    archive: Path, root: Path, deadline: float, details: dict
) -> list[Path]:
    months, truncated, error = _bounded_runtime_entries(
        archive, root, limit=121, deadline=deadline
    )
    if error:
        raise OSError("archive month scan failed")
    months = [month for month in months if _archive_month_directory(month, root)]
    if not _months_truncated(truncated, months):
        return months
    details["codes"].append("archive_scan_truncated")
    details["deletion_codes"].append("archive_state_unknown")
    return months[:120]


def _months_truncated(truncated: bool, months: list[Path]) -> bool:
    return truncated or len(months) > 120


def _is_bag_directory(item: Path, root: Path) -> bool:
    return generation_maintenance.safe_kind(item, root)[0] == "directory" and item.name.startswith("bag-")


def _record_bag_overflow(details: dict, bags: list[Path]) -> None:
    details["codes"].append("archive_scan_truncated")
    details["deletion_codes"].append("archive_state_unknown")
    del bags[MAX_RUNTIME_ENTRIES:]


def _collect_month_bags(
    month: Path, root: Path, deadline: float, details: dict, bags: list[Path]
) -> None:
    entries, truncated, error = _bounded_runtime_entries(
        month,
        root,
        limit=MAX_RUNTIME_ENTRIES + 1,
        deadline=deadline,
    )
    if error:
        raise OSError("archive bag scan failed")
    if truncated:
        details["codes"].append("archive_scan_truncated")
        details["deletion_codes"].append("archive_state_unknown")
    _append_bag_directories(entries, root, details, bags)


def _append_bag_directories(
    entries: list[Path], root: Path, details: dict, bags: list[Path]
) -> None:
    for item in entries:
        if _is_bag_directory(item, root):
            bags.append(item)
        if len(bags) > MAX_RUNTIME_ENTRIES:
            _record_bag_overflow(details, bags)
            return


def _archive_bags(
    months: list[Path], root: Path, deadline: float, details: dict
) -> list[Path]:
    bags: list[Path] = []
    for month in months:
        _collect_month_bags(month, root, deadline, details, bags)
        if len(bags) >= MAX_RUNTIME_ENTRIES:
            return bags[:MAX_RUNTIME_ENTRIES]
    return bags


def _archive_manifest_key(bag: Path, archive: Path, details: dict) -> tuple | None:
    manifest, problem = read_bounded_json(
        bag / "archive-manifest.json", archive, max_bytes=MAX_MANIFEST_BYTES
    )
    if problem or not isinstance(manifest, dict):
        details["codes"].append("archive_manifest_invalid")
        return None
    return manifest.get("logical_daily_id"), manifest.get("source_hash")


def _scan_archive_manifests(
    bags: list[Path], archive: Path, root: Path, deadline: float, details: dict
) -> set[str]:
    """Bag paths that carry a readable manifest, counting duplicates on the way."""
    seen: set[tuple[object, object]] = set()
    bag_paths: set[str] = set()
    for bag in bags:
        if monotonic_deadline.deadline_reached(deadline):
            raise TimeoutError("archive check deadline")
        key = _archive_manifest_key(bag, archive, details)
        if key is None:
            continue
        details["duplicates"] += int(key in seen)
        seen.add(key)
        bag_paths.add(bag.relative_to(root).as_posix())
    return bag_paths


def _bag_path_entry(item: object) -> bool:
    return isinstance(item, dict) and isinstance(item.get("bag_path"), str)


def _index_validity(index: dict, bag_paths: set[str]) -> str:
    if index.get("schema_version") != "archive-index/v1":
        return "invalid"
    indexed = index.get("bags", [])
    indexed_paths = {
        str(item.get("bag_path")) for item in indexed if _bag_path_entry(item)
    }
    if _index_paths_agree(indexed_paths, bag_paths, indexed):
        return "valid"
    return "invalid"


def _index_paths_agree(indexed_paths: set[str], bag_paths: set[str], indexed) -> bool:
    return indexed_paths == bag_paths and len(indexed_paths) == len(indexed)


def _archive_index_kind_state(index_kind: str) -> str | None:
    """The index state settled by the entry alone, or None to read the file."""
    if index_kind == "missing":
        return "missing"
    if index_kind != "regular":
        return "invalid"
    return None


def _archive_index_state(
    archive: Path, root: Path, bag_paths: set[str], deadline: float
) -> str:
    index_path = archive / "archive-index.json"
    unreadable = _archive_index_kind_state(generation_maintenance.safe_kind(index_path, root)[0])
    if unreadable is not None:
        return unreadable
    index, problem = read_bounded_json(
        index_path,
        archive,
        max_bytes=MAX_MANIFEST_BYTES,
        deadline=deadline,
    )
    if problem or not isinstance(index, dict):
        return "invalid"
    return _index_validity(index, bag_paths)


def _scan_archive(
    archive: Path, root: Path, deadline: float, details: dict
) -> None:
    months = _archive_months(archive, root, deadline, details)
    bags = _archive_bags(months, root, deadline, details)
    details["bags"] = len(bags)
    bag_paths = _scan_archive_manifests(bags, archive, root, deadline, details)
    details["index"] = _archive_index_state(archive, root, bag_paths, deadline)


def _archive_problem(details: dict) -> bool:
    if details["duplicates"] or details["quarantined"]:
        return True
    return bool(details["codes"]) or details["index"] == "invalid"


def _archive_result(details: dict) -> dict:
    problem = _archive_problem(details)
    status = "degraded" if problem else "ok"
    if details["codes"]:
        status = "error"
    message = "Archive state is healthy."
    if problem:
        message = "Archive state requires operator attention."
    return generation_maintenance.result("archives", status, message, details)


def archive_check(root: Path, state_root: Path, deadline: float = float("inf")) -> dict:
    archive = archive_path(root)
    details = _empty_archive_details()
    _record_quarantine_state(state_root, deadline, details)
    archive_kind = generation_maintenance.safe_kind(archive, root)[0]
    if archive_kind == "missing":
        return generation_maintenance.result("archives", "ok", "No archive exists.", details)
    if archive_kind != "directory":
        details["read_error"] = True
        details["deletion_codes"].append("archive_state_unreadable")
        return generation_maintenance.result("archives", "error", "Archive root is unsafe.", details)
    try:
        _scan_archive(archive, root, deadline, details)
    except (OSError, TimeoutError):
        details["codes"].append("archive_unreadable")
        details["read_error"] = True
        details["deletion_codes"].append("archive_state_unreadable")
    return _archive_result(details)


def count_claims(database: sqlite3.Connection, details: dict) -> None:
    details["claims"] = len(
        database.execute(
            "SELECT 1 FROM claim LIMIT ?", (MAX_OPERATIONAL_ROWS + 1,)
        ).fetchall()
    )
    rows = database.execute(
        "SELECT code, page FROM claim_index_diagnostic ORDER BY code, page LIMIT ?",
        (MAX_OPERATIONAL_ROWS + 1,),
    ).fetchall()
    details["diagnostics"] = min(len(rows), MAX_OPERATIONAL_ROWS)
    details["codes"] = sorted({str(row[0]) for row in rows})
    details["by_code"] = _claims_by_code(rows)
    pages = sorted({str(row[1]) for row in rows})
    details["pages"] = pages[:MAX_CLAIM_PAGES_NAMED]
    details["pages_omitted"] = len(pages) - len(details["pages"])
    if len(rows) > MAX_OPERATIONAL_ROWS or details["claims"] > MAX_OPERATIONAL_ROWS:
        details["codes"].append("claim_scan_truncated")


def claim_status(details: dict) -> str:
    if details["index"] == "invalid":
        return "error"
    if details["diagnostics"]:
        return "degraded"
    return "ok"


# A display bound on the pages named in the claims finding (the remedy text above is
# per page); the rest are counted in `pages_omitted`, never dropped silently.
MAX_CLAIM_PAGES_NAMED = 8


def _claims_by_code(rows: list) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row[0])] = counts.get(str(row[0]), 0) + 1
    return counts
