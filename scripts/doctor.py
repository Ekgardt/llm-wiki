"""Agent-readable local health checks and conservative repairs."""

from __future__ import annotations

import argparse
import errno
import functools
import hashlib
import importlib.util
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

import reliable_memory
from bounded_io import read_stable_bytes
from install_control import InstallControlError, validate_install_state
from reliable_memory import (
    open_readonly_operational_db,
    read_runtime_bytes,
)

try:
    import tomllib as STDLIB_TOML
except ModuleNotFoundError:  # Python 3.10
    STDLIB_TOML = None

try:
    import tomli as TOMLI
except ModuleNotFoundError:  # Python 3.11+ does not install the backport
    TOMLI = None

SCHEMA_VERSION = "1.0"
INDEX_FRESH_SECONDS = 24 * 60 * 60
STALE_LEASE_SECONDS = 10 * 60
PERMANENT_FAILURE_ATTEMPTS = 5
SUMMARY_LIMIT = 600
VALID_STATUSES = ("ok", "degraded", "error", "skipped")
VALID_REPAIR_ACTIONS = frozenset(
    {"runtime", "transactions", "queue", "indexes", "archives", "generations"}
)
RUNTIME_DIRECTORIES = ("run", "logs", "cache")
MAX_QUEUE_FILES = 200
MAX_QUEUE_FILE_BYTES = 64 * 1024
MAX_CONFIG_BYTES = 64 * 1024
MAX_STATE_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_INDEX_PATHS = 10_000
MAX_INDEX_DB_BYTES = 1024 * 1024 * 1024
MAX_LOCK_BYTES = 4096
MAX_QUEUE_RESULT_BYTES = 8 * 1024 * 1024
MAX_OPERATIONAL_DB_BYTES = 256 * 1024 * 1024
MAX_OPERATIONAL_ROWS = 10_000
MAX_RUNTIME_ENTRIES = 10_000
LOCK_STALE_SECONDS = 10 * 60
DEFAULT_TIME_BUDGET_SECONDS = 5.0
# A commit on a rollback-journal database locks readers out for milliseconds.
# Wait that out rather than reporting a healthy database as unreadable, but
# stay far below the default time budget above.
READ_BUSY_MS = 250
DEFAULT_GENERATION_TIME_BUDGET_SECONDS = 60.0
DEFAULT_GENERATION_SOURCE_LIMIT = 10_000
GENERATION_FRESH_SECONDS = 24 * 60 * 60
CODEX_HOOK_PROBE_SECONDS = 2.0
CODEX_HOOK_PROBE_STARTUP_SECONDS = 0.25
MAX_CODEX_HOOK_PROBE_BYTES = 256 * 1024
_CODEX_PROBE_NOT_COMPLETED = object()
INDEX_COLUMNS = {"path", "title", "summary", "body", "project", "timestamp", "slug"}
TRANSACTION_STATES = (
    "preparing",
    "prepared",
    "applying",
    "committed",
    "discarded",
    "conflicted",
    "quarantined",
)
QUEUE_STATES = ("ready", "leased", "blocked", "succeeded", "dead", "cancelled")
UNDO_RETENTION_DAYS = 30
MAINTENANCE_LEASE_SECONDS = 120
MAINTENANCE_HEARTBEAT_SECONDS = 40.0
FILESYSTEM_PROBE_SECONDS = 1.0
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


def _as_utc(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _result(check_id: str, status: str, message: str, details: dict) -> dict:
    return {"id": check_id, "status": status, "message": message, "details": details}


def _environment_check(root: Path, state_root: Path) -> dict:
    python_ok = tuple(sys.version_info[:2]) >= (3, 10)
    root_ok = root.is_dir()
    state_parent_ok = state_root.is_dir()
    layout = {
        "knowledge": (root / "knowledge").is_dir(),
        "knowledge_notes": (root / "knowledge" / "notes").is_dir(),
        "scripts": (root / "scripts").is_dir(),
    }
    status = "ok" if python_ok and root_ok and state_parent_ok and all(layout.values()) else "error"
    details = {
        "python": {
            "status": "ok" if python_ok else "error",
            "version": ".".join(str(part) for part in sys.version_info[:3]),
        },
        "vault_root": {"status": "ok" if root_ok else "error"},
        "state_root": {"status": "ok" if state_parent_ok else "error"},
        "layout": layout,
    }
    message = (
        "Configured roots and source layout are available."
        if status == "ok"
        else "Configured environment is incomplete."
    )
    return _result("environment", status, message, details)


def _is_writable_directory(directory: Path) -> bool:
    """Check declared writability without creating or modifying anything."""
    if not directory.is_dir():
        return False
    try:
        mode = directory.stat().st_mode
    except OSError:
        return False
    write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    return bool(mode & write_bits) and os.access(directory, os.W_OK)


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _safe_kind(path: Path, root: Path) -> tuple[str, os.stat_result | None]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "unsafe", None
    if stat.S_ISLNK(info.st_mode):
        return "symlink", info
    if not _within(path, root):
        return "outside", info
    if stat.S_ISDIR(info.st_mode):
        return "directory", info
    if stat.S_ISREG(info.st_mode):
        return "regular", info
    return "special", info


def _runtime_directory_state(state_root: Path) -> tuple[dict[str, object], tuple[int, int, int]]:
    details: dict[str, object] = {}
    counts = [0, 0, 0]
    for relative in RUNTIME_DIRECTORIES:
        path = state_root / relative
        kind, _ = _safe_kind(path, state_root)
        exists = kind == "directory"
        writable = exists and _is_writable_directory(path)
        details[relative] = {
            "exists": kind != "missing",
            "writable": writable,
            "symlink": kind == "symlink",
            "safe": kind in {"missing", "directory"},
        }
        counts[0] += not exists
        counts[1] += exists and not writable
        counts[2] += kind not in {"missing", "directory"}
    return details, (counts[0], counts[1], counts[2])


def _runtime_directory_result(counts: tuple[int, int, int]) -> tuple[str, str]:
    missing, unwritable, unsafe = counts
    if unsafe:
        return "error", "Runtime paths include unsafe entries."
    if unwritable:
        return "error", "Runtime directories are not writable."
    if missing:
        return "degraded", f"{missing} runtime directories are missing."
    return "ok", "Runtime directories exist and are writable."


def _merge_install_health(
    status: str,
    message: str,
    details: dict[str, object],
    install: dict[str, object],
) -> tuple[str, str]:
    details["install"] = {
        "health": install["health"],
        "status": install["status"],
    }
    details["codes"] = install["codes"]
    ranks = {"ok": 0, "degraded": 1, "error": 2}
    install_health = str(install["health"])
    if ranks[install_health] > ranks[status]:
        return install_health, f"Install ownership state is {install['status']}."
    return status, message


def _runtime_check(state_root: Path) -> dict:
    details, counts = _runtime_directory_state(state_root)
    status, message = _runtime_directory_result(counts)
    install = validate_install_state(state_root)
    status, message = _merge_install_health(status, message, details, install)
    return _result("runtime", status, message, details)


def _bounded_json_path_problem(
    path: Path,
    root: Path,
    max_bytes: int,
    deadline: float,
) -> str | None:
    if time.monotonic() >= deadline:
        return "budget"
    if _safe_kind(path, root)[0] != "regular":
        return "unsafe"
    try:
        oversized = path.lstat().st_size > max_bytes
    except OSError:
        return "invalid"
    if oversized:
        return "oversized"
    return None


def _read_bounded_json(
    path: Path,
    root: Path,
    *,
    max_bytes: int = MAX_QUEUE_FILE_BYTES,
    expected_type: type = dict,
    deadline: float = float("inf"),
) -> tuple[Any | None, str | None]:
    problem = _bounded_json_path_problem(path, root, max_bytes, deadline)
    if problem:
        return None, problem
    try:
        raw = read_runtime_bytes(path, root, max_bytes=max_bytes)
        if time.monotonic() >= deadline:
            return None, "budget"
        value = json.loads(raw.decode("utf-8"))
        return (value, None) if isinstance(value, expected_type) else (None, "invalid")
    except (OSError, PermissionError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid"


def _lease_state(task: dict, now: datetime) -> tuple[bool, bool]:
    pid = task.get("lease_pid")
    token = task.get("lease_token")
    try:
        acquired = datetime.fromisoformat(str(task.get("lease_acquired_at", "")))
        if acquired.tzinfo is None:
            acquired = acquired.replace(tzinfo=timezone.utc)
        stale = (now - acquired.astimezone(timezone.utc)).total_seconds() > STALE_LEASE_SECONDS
    except (TypeError, ValueError):
        stale = True
    owned = isinstance(pid, int) and pid > 0 and isinstance(token, str) and bool(token)
    return stale, owned


def _queue_artifact_state(state_root: Path, deadline: float) -> dict[str, Any]:
    details: dict[str, Any] = {
        "legacy_retained": 0,
        "legacy_malformed": 0,
        "results_retained": 0,
        "queue_quarantined": 0,
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
    _count_legacy_queue_entries(entries, legacy, deadline, details)
    for key, relative in (
        ("results_retained", "run/queue-results"),
        ("queue_quarantined", "run/queue-quarantine"),
    ):
        _count_queue_artifact_directory(
            state_root, relative, key, deadline, details
        )
    _append_queue_artifact_codes(details)
    return details


def _count_legacy_queue_entries(
    entries: list[Path], legacy: Path, deadline: float, details: dict
) -> None:
    for path in entries:
        if path.suffix not in {".json", ".processing"}:
            details["artifact_error"] = True
            continue
        details["legacy_retained"] += 1
        _value, problem = _read_bounded_json(path, legacy, deadline=deadline)
        if problem:
            details["legacy_malformed"] += 1


def _count_queue_artifact_directory(
    state_root: Path, relative: str, key: str, deadline: float, details: dict
) -> None:
    entries, truncated, error = _bounded_runtime_entries(
        state_root / relative,
        state_root,
        limit=MAX_RUNTIME_ENTRIES,
        deadline=deadline,
    )
    details[key] = len(entries)
    details["artifact_truncated"] |= truncated
    details["artifact_error"] |= error
    if _any_irregular_entry(entries, state_root):
        details["artifact_error"] = True


def _append_queue_artifact_codes(details: dict) -> None:
    for key, code in (
        ("legacy_retained", "legacy_queue_retained"),
        ("legacy_malformed", "legacy_queue_malformed"),
        ("results_retained", "queue_result_retained"),
        ("queue_quarantined", "queue_quarantine_retained"),
    ):
        if details[key]:
            details["deletion_codes"].append(code)
    if details["artifact_error"] or details["artifact_truncated"]:
        details["deletion_codes"].append("queue_artifact_state_unknown")


def _unreadable_queue_result(state_root: Path, deadline: float, message: str) -> dict:
    details = _queue_artifact_state(state_root, deadline)
    details.update(read_error=True, states={state: 0 for state in QUEUE_STATES})
    details["deletion_codes"].append("queue_state_unreadable")
    return _result("queue", "error", message, details)


def _new_queue_counts() -> dict:
    return {
        "pending": 0,
        "permanently_failed": 0,
        "stale_leases": 0,
        "ownerless_leases": 0,
        "unsafe_entries": 0,
        "oversized_entries": 0,
        "scanned": 0,
        "truncated": False,
    }


def _count_stale_processing(entry: os.DirEntry, now: datetime, counts: dict) -> None:
    try:
        modified = entry.stat(follow_symlinks=False).st_mtime
    except OSError:
        counts["unsafe_entries"] += 1
        return
    if now.timestamp() - modified <= STALE_LEASE_SECONDS:
        return
    counts["stale_leases"] += 1
    counts["ownerless_leases"] += 1


def _count_problem_entry(
    entry: os.DirEntry, problem: str, now: datetime, counts: dict
) -> None:
    counts["unsafe_entries"] += problem == "unsafe"
    counts["oversized_entries"] += problem == "oversized"
    counts["truncated"] = counts["truncated"] or problem == "oversized"
    if problem == "invalid" and entry.name.endswith(".json"):
        counts["permanently_failed"] += 1
        return
    if entry.name.endswith(".processing"):
        _count_stale_processing(entry, now, counts)


def _count_pending_task(task: dict, counts: dict) -> None:
    counts["pending"] += 1
    try:
        attempts = int(task.get("attempts", 0))
    except (ValueError, TypeError):
        counts["permanently_failed"] += 1
        return
    counts["permanently_failed"] += attempts >= PERMANENT_FAILURE_ATTEMPTS


def _scan_queue_entry(
    entry: os.DirEntry, queue: Path, now: datetime, counts: dict
) -> None:
    task, problem = _read_bounded_json(Path(entry.path), queue)
    if problem:
        _count_problem_entry(entry, problem, now, counts)
        return
    if entry.name.endswith(".json"):
        _count_pending_task(task, counts)
        return
    stale, owned = _lease_state(task, now)
    counts["stale_leases"] += stale
    counts["ownerless_leases"] += stale and not owned


def _queue_entry_of_interest(entry: os.DirEntry) -> bool:
    return entry.name.endswith(".json") or entry.name.endswith(".processing")


def _scan_queue_entries(
    entries, queue: Path, now: datetime, deadline: float, counts: dict
) -> None:
    for entry in entries:
        if counts["scanned"] >= MAX_QUEUE_FILES or time.monotonic() >= deadline:
            counts["truncated"] = True
            return
        counts["scanned"] += 1
        if _queue_entry_of_interest(entry):
            _scan_queue_entry(entry, queue, now, counts)


def _scan_legacy_queue(
    state_root: Path, now: datetime, deadline: float, counts: dict
) -> None:
    queue = state_root / "run" / "queue"
    kind, _ = _safe_kind(queue, state_root)
    if kind == "missing":
        return
    if kind != "directory":
        counts["unsafe_entries"] += 1
        return
    try:
        with os.scandir(queue) as entries:
            _scan_queue_entries(entries, queue, now, deadline, counts)
    except OSError:
        counts["unsafe_entries"] += 1


def _legacy_queue_degraded(counts: dict) -> bool:
    return any(
        counts[key]
        for key in (
            "pending",
            "stale_leases",
            "unsafe_entries",
            "oversized_entries",
            "truncated",
        )
    )


def _legacy_queue_status(counts: dict) -> tuple[str, str]:
    if counts["permanently_failed"]:
        return (
            "error",
            f"Queue has {counts['permanently_failed']} permanently failed task(s).",
        )
    if _legacy_queue_degraded(counts):
        return (
            "degraded",
            f"Queue has {counts['pending']} pending task(s) and "
            f"{counts['stale_leases']} stale lease(s).",
        )
    return "ok", "Queue has no pending or stale work."


def _adjusted_queue_status(status: str, artifacts: dict) -> str:
    if not artifacts["deletion_codes"]:
        return status
    if artifacts["artifact_error"]:
        return "error"
    if status == "ok":
        return "degraded"
    return status


def _legacy_queue_result(state_root: Path, now: datetime, deadline: float) -> dict:
    counts = _new_queue_counts()
    _scan_legacy_queue(state_root, now, deadline, counts)
    details = dict(counts, read_error=False)
    artifacts = _queue_artifact_state(state_root, deadline)
    details.update(artifacts)
    status, message = _legacy_queue_status(counts)
    return _result("queue", _adjusted_queue_status(status, artifacts), message, details)


def _queue_check(state_root: Path, now: datetime, deadline: float) -> dict:
    database_path = state_root / "run" / "queue.sqlite3"
    database_kind = _safe_kind(database_path, state_root)[0]
    if database_kind == "regular":
        return _queue_v2_check(state_root, now, deadline)
    if database_kind != "missing":
        return _unreadable_queue_result(
            state_root, deadline, "Queue database is unsafe."
        )
    if _database_sidecar_present(database_path, state_root):
        return _unreadable_queue_result(
            state_root, deadline, "Queue sidecars lack a database."
        )
    return _legacy_queue_result(state_root, now, deadline)


def _read_busy_ms(deadline: float | None) -> int:
    """Wait out a brief commit lock, keeping budget left to report what happened.

    Spending the whole remaining budget on the wait would turn every busy
    database into an indistinguishable "budget exhausted" verdict.
    """
    if deadline is None or not math.isfinite(deadline):
        return READ_BUSY_MS
    remaining_ms = (deadline - time.monotonic()) * 1000
    return max(0, min(READ_BUSY_MS, int(remaining_ms / 2)))


def _readonly_database(
    path: Path,
    state_root: Path,
    *,
    max_bytes: int = MAX_OPERATIONAL_DB_BYTES,
    deadline: float | None = None,
) -> sqlite3.Connection:
    return open_readonly_operational_db(
        path,
        state_root,
        max_bytes=max_bytes,
        owner_only=False,
        busy_ms=_read_busy_ms(deadline),
    )


def _deadline_reached(deadline: float) -> bool:
    return time.monotonic() >= deadline


def _bounded_runtime_entries(
    directory: Path,
    root: Path,
    *,
    limit: int,
    deadline: float,
) -> tuple[list[Path], bool, bool]:
    kind, _ = _safe_kind(directory, root)
    if kind == "missing":
        return [], False, False
    if kind != "directory":
        return [], False, True
    entries: list[Path] = []
    try:
        with os.scandir(directory) as scanned:
            for entry in scanned:
                if _deadline_reached(deadline) or len(entries) >= limit:
                    return entries, True, False
                entries.append(Path(entry.path))
    except OSError:
        return entries, False, True
    return entries, False, False


def _tables(database: sqlite3.Connection, deadline: float = float("inf")) -> set[str]:
    if _deadline_reached(deadline):
        raise TimeoutError("database schema deadline")
    rows = database.execute(
        "SELECT name FROM sqlite_master WHERE type='table' LIMIT 257"
    ).fetchall()
    if len(rows) > 256:
        raise sqlite3.DatabaseError("database schema exceeds table limit")
    return {str(row[0]) for row in rows}


def _columns(
    database: sqlite3.Connection,
    table: str,
    deadline: float = float("inf"),
) -> set[str]:
    if _deadline_reached(deadline):
        raise TimeoutError("database schema deadline")
    rows = database.execute(f'PRAGMA table_info("{table}")').fetchmany(257)
    if len(rows) > 256:
        raise sqlite3.DatabaseError("database schema exceeds column limit")
    return {str(row[1]) for row in rows}


def _parse_utc(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _live_owner(row: sqlite3.Row, now: datetime, *, pid_column: str) -> bool:
    columns = set(row.keys())
    pid = row[pid_column] if pid_column in columns else None
    expiry = _parse_utc(row["expires_at"]) if "expires_at" in columns else None
    pid_live = isinstance(pid, int) and pid > 0 and _pid_alive(pid)
    unexpired = expiry is not None and expiry > now
    return pid_live or unexpired


def _owner_row_known(row: sqlite3.Row, *, pid_column: str) -> bool:
    columns = set(row.keys())
    pid = row[pid_column] if pid_column in columns else None
    expiry = _parse_utc(row["expires_at"]) if "expires_at" in columns else None
    return isinstance(pid, int) and pid > 0 or expiry is not None


def _archive_path(root: Path) -> Path:
    return root / "knowledge" / "daily" / "archive"


def _database_sidecar_present(path: Path, state_root: Path) -> bool:
    return any(
        _safe_kind(Path(f"{path}{suffix}"), state_root)[0] != "missing"
        for suffix in ("-journal", "-wal", "-shm")
    )


def _transaction_artifacts(state_root: Path, deadline: float) -> tuple[set[str], bool]:
    entries, truncated, error = _bounded_runtime_entries(
        state_root / "run" / "transactions",
        state_root,
        limit=MAX_RUNTIME_ENTRIES,
        deadline=deadline,
    )
    identifiers: set[str] = set()
    unsafe = truncated or error
    for entry in entries:
        if (
            _safe_kind(entry, state_root)[0] != "directory"
            or re.fullmatch(r"[0-9a-z_-]{1,128}", entry.name) is None
        ):
            unsafe = True
        else:
            identifiers.add(entry.name)
    return identifiers, unsafe


_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_TRANSACTION_ID_RE = re.compile(r"[0-9a-z_-]{1,128}")
_TRANSACTION_QUERY = (
    "SELECT id, operation_id, request_hash, state, preconditions_json, "
    "plan_hash, created_at, updated_at, artifacts_pruned_at "
    'FROM "transaction"'
)
_OPERATION_QUERY = (
    "SELECT transaction_id, position, kind, path, before_hash, "
    'after_hash, parent_device, parent_inode, applied FROM "operation"'
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
    operation_rows: list[sqlite3.Row], known_ids: set[str]
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


def _valid_plan_hash(row: sqlite3.Row, state: str) -> bool:
    if state == "preparing" and row["plan_hash"] == "":
        return True
    return _is_digest(row["plan_hash"])


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
    created = _parse_utc(row["created_at"])
    updated = _parse_utc(row["updated_at"])
    if created is None or updated is None or created > updated:
        return False
    if row["artifacts_pruned_at"] is None:
        return True
    return _parse_utc(row["artifacts_pruned_at"]) is not None


def _valid_transaction_row(row: sqlite3.Row, state: str) -> bool:
    return (
        _valid_transaction_identity(row)
        and _valid_transaction_payload(row, state)
        and _valid_transaction_timestamps(row)
    )


def _transaction_row_corrupt(
    row: sqlite3.Row, state: str, operation_positions: dict[str, list[int]]
) -> bool:
    if not _valid_transaction_row(row, state):
        return True
    positions = operation_positions.get(row["id"], [])
    if positions != list(range(len(positions))):
        return True
    return state not in {"preparing", "discarded"} and not positions


def _committed_within_undo_window(
    row: sqlite3.Row, state: str, cutoff: datetime
) -> bool:
    if state != "committed" or row["artifacts_pruned_at"] is not None:
        return False
    updated = _parse_utc(row["updated_at"])
    return updated is not None and updated >= cutoff


def _undo_artifact_retained(
    row: sqlite3.Row, state: str, cutoff: datetime, state_root: Path
) -> bool:
    if not _committed_within_undo_window(row, state, cutoff):
        return False
    transaction_id = row["id"]
    if _TRANSACTION_ID_RE.fullmatch(transaction_id) is None:
        return False
    artifact = state_root / "run" / "transactions" / transaction_id
    return _safe_kind(artifact, state_root)[0] == "directory"


def _collect_error_code(
    database: sqlite3.Connection,
    row: sqlite3.Row,
    state: str,
    transaction_columns: set[str],
    codes: set[str],
) -> None:
    if state not in {"conflicted", "quarantined"}:
        return
    if "error_code" not in transaction_columns:
        return
    code_row = database.execute(
        'SELECT error_code FROM "transaction" WHERE id=?', (row["id"],)
    ).fetchone()
    if code_row is not None and code_row[0]:
        codes.add(str(code_row[0]))


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
) -> bool:
    """Count one row and report whether it is corrupt."""
    state = row["state"]
    if not isinstance(state, str) or state not in TRANSACTION_STATES:
        details["deletion_codes"].append("transaction_state_unknown")
        return False
    states[state] += 1
    _collect_error_code(database, row, state, transaction_columns, codes)
    if _undo_artifact_retained(row, state, cutoff, state_root):
        details["undo_retained"] += 1
    return _transaction_row_corrupt(row, state, operation_positions)


def _scan_transaction_rows(
    database: sqlite3.Connection,
    transaction_rows: list[sqlite3.Row],
    operation_positions: dict[str, list[int]],
    transaction_columns: set[str],
    *,
    state_root: Path,
    now: datetime,
    deadline: float,
    details: dict,
    states: dict[str, int],
) -> tuple[set[str], bool]:
    codes: set[str] = set()
    corrupt = False
    cutoff = now - timedelta(days=UNDO_RETENTION_DAYS)
    for row in transaction_rows:
        if _deadline_reached(deadline):
            raise TimeoutError("transaction check deadline")
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
        ) or corrupt
    return codes, corrupt


def _bounded_operational_rows(
    database: sqlite3.Connection, query: str, details: dict, truncation_code: str
) -> list[sqlite3.Row]:
    rows = database.execute(
        query + " LIMIT ?", (MAX_OPERATIONAL_ROWS + 1,)
    ).fetchall()
    if len(rows) <= MAX_OPERATIONAL_ROWS:
        return rows
    details["codes"].append(truncation_code)
    details["deletion_codes"].append("transaction_state_unknown")
    return rows[:MAX_OPERATIONAL_ROWS]


def _lease_live(value: object, now: datetime) -> bool:
    return (_parse_utc(value) or datetime.max.replace(tzinfo=timezone.utc)) > now


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
    return not _owner_row_known(row, pid_column="process_id")


def _any_owner_row_unknown(rows: list[sqlite3.Row], require_token: bool) -> bool:
    return any(_owner_row_unknown(row, require_token=require_token) for row in rows)


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
) -> None:
    if table not in tables:
        return
    rows = database.execute(
        _OWNER_TABLE_QUERIES[table], (MAX_OPERATIONAL_ROWS + 1,)
    ).fetchall()
    if len(rows) > MAX_OPERATIONAL_ROWS:
        details["deletion_codes"].append(unknown_code)
    bounded = rows[:MAX_OPERATIONAL_ROWS]
    if _any_owner_row_unknown(bounded, require_token):
        details["deletion_codes"].append(unknown_code)
    details[count_key] = sum(
        _live_owner(row, now, pid_column="process_id") for row in bounded
    )


def _count_owner_tables(
    database: sqlite3.Connection, tables: set[str], details: dict, now: datetime
) -> None:
    _count_project_leases(database, tables, details, now)
    _count_owner_table(
        database,
        tables,
        details,
        now,
        table="writer_owners",
        unknown_code="writer_state_unknown",
        count_key="live_writers",
        require_token=False,
    )
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
    row: sqlite3.Row, artifacts: set[str], known_ids: set[str]
) -> bool:
    transaction_id = row["id"]
    if not isinstance(transaction_id, str) or transaction_id not in known_ids:
        return True
    expected = row["state"] != "discarded" and row["artifacts_pruned_at"] is None
    return (transaction_id in artifacts) != expected


def _artifacts_inconsistent(
    state_root: Path,
    transaction_rows: list[sqlite3.Row],
    known_ids: set[str],
    details: dict,
    deadline: float,
) -> bool:
    artifacts, unsafe_artifacts = _transaction_artifacts(state_root, deadline)
    if unsafe_artifacts or artifacts - known_ids:
        details["deletion_codes"].append("transaction_artifact_state_unknown")
    return any(
        _artifact_mismatch(row, artifacts, known_ids) for row in transaction_rows
    )


def _known_transaction_ids(rows: list[sqlite3.Row]) -> set[str]:
    return {row["id"] for row in rows if isinstance(row["id"], str)}


def _scan_transaction_tables(
    database: sqlite3.Connection,
    tables: set[str],
    transaction_columns: set[str],
    *,
    state_root: Path,
    now: datetime,
    deadline: float,
    details: dict,
    states: dict[str, int],
) -> None:
    transaction_rows = _bounded_operational_rows(
        database, _TRANSACTION_QUERY, details, "transaction_scan_truncated"
    )
    operation_rows = _bounded_operational_rows(
        database, _OPERATION_QUERY, details, "transaction_operation_scan_truncated"
    )
    known_ids = _known_transaction_ids(transaction_rows)
    operation_positions, corrupt = _operation_positions(operation_rows, known_ids)
    codes, rows_corrupt = _scan_transaction_rows(
        database,
        transaction_rows,
        operation_positions,
        transaction_columns,
        state_root=state_root,
        now=now,
        deadline=deadline,
        details=details,
        states=states,
    )
    details["codes"] = sorted(set(details["codes"]) | codes)
    _count_owner_tables(database, tables, details, now)
    inconsistent = _artifacts_inconsistent(
        state_root, transaction_rows, known_ids, details, deadline
    )
    if corrupt or rows_corrupt or inconsistent:
        details["codes"].append("transaction_metadata_corrupt")
        details["deletion_codes"].append("transaction_state_corrupt")


def _operation_columns(
    database: sqlite3.Connection, tables: set[str], deadline: float
) -> set[str]:
    if "operation" not in tables:
        return set()
    return _columns(database, "operation", deadline)


def _transaction_schema_complete(
    transaction_columns: set[str], operation_columns: set[str]
) -> bool:
    return TRANSACTION_REQUIRED_COLUMNS.issubset(
        transaction_columns
    ) and OPERATION_REQUIRED_COLUMNS.issubset(operation_columns)


def _scan_transaction_database(
    path: Path,
    state_root: Path,
    now: datetime,
    deadline: float,
    details: dict,
    states: dict[str, int],
) -> dict | None:
    """Fill in the counters; return a result only when the schema is incomplete."""
    with _readonly_database(path, state_root, deadline=deadline) as database:
        if _deadline_reached(deadline):
            raise TimeoutError("transaction check deadline")
        tables = _tables(database, deadline)
        if "transaction" not in tables:
            raise sqlite3.DatabaseError("transaction table missing")
        transaction_columns = _columns(database, "transaction", deadline)
        operation_columns = _operation_columns(database, tables, deadline)
        if not _transaction_schema_complete(transaction_columns, operation_columns):
            details["codes"].append("transaction_metadata_missing")
            details["deletion_codes"].append("transaction_state_corrupt")
            return _result(
                "transactions",
                "error",
                "Transaction metadata is incomplete.",
                details,
            )
        _scan_transaction_tables(
            database,
            tables,
            transaction_columns,
            state_root=state_root,
            now=now,
            deadline=deadline,
            details=details,
            states=states,
        )
        return None


def _unreadable_transactions(details: dict, message: str) -> dict:
    details["read_error"] = True
    details["deletion_codes"].append("transaction_state_unreadable")
    return _result("transactions", "error", message, details)


def _missing_transaction_database(
    path: Path, state_root: Path, details: dict, deadline: float
) -> dict:
    artifacts, unsafe = _transaction_artifacts(state_root, deadline)
    if artifacts or unsafe or _database_sidecar_present(path, state_root):
        return _unreadable_transactions(
            details, "Transaction artifacts lack readable state."
        )
    return _result("transactions", "ok", "No transaction database exists.", details)


def _transaction_status(states: dict[str, int], problem: int, invalid: bool) -> str:
    if states["conflicted"] or states["quarantined"] or invalid:
        return "error"
    if problem:
        return "degraded"
    return "ok"


def _append_state_deletion_codes(details: dict, states: dict[str, int]) -> None:
    if any(states.get(state, 0) for state in ("preparing", "prepared", "applying")):
        details["deletion_codes"].append("transaction_nonterminal")
    if states["conflicted"]:
        details["deletion_codes"].append("transaction_conflicted")
    if states["quarantined"]:
        details["deletion_codes"].append("transaction_quarantined")


def _append_live_deletion_codes(details: dict) -> None:
    for key, code in (
        ("undo_retained", "transaction_undo_retained"),
        ("live_project_leases", "project_lease_live"),
        ("live_writers", "writer_live"),
        ("live_maintenance_owners", "maintenance_owner_live"),
    ):
        if details[key]:
            details["deletion_codes"].append(code)


def _transaction_result(details: dict, states: dict[str, int]) -> dict:
    details["codes"] = sorted(set(details["codes"]))
    details["deletion_codes"] = list(dict.fromkeys(details["deletion_codes"]))
    problem = (
        sum(states[state] for state in ("preparing", "prepared", "applying"))
        + states["conflicted"]
        + states["quarantined"]
    )
    invalid_state = any(
        code in details["deletion_codes"]
        for code in ("transaction_state_unknown", "transaction_state_corrupt")
    )
    _append_state_deletion_codes(details, states)
    _append_live_deletion_codes(details)
    message = "Transaction state is healthy."
    if problem or invalid_state:
        message = "Transaction state requires operator attention."
    return _result(
        "transactions",
        _transaction_status(states, problem, invalid_state),
        message,
        details,
    )


def _empty_transaction_details() -> tuple[dict, dict[str, int]]:
    states = {state: 0 for state in TRANSACTION_STATES}
    details: dict[str, Any] = {
        "states": states,
        "codes": [],
        "undo_retained": 0,
        "live_project_leases": 0,
        "live_writers": 0,
        "live_maintenance_owners": 0,
        "read_error": False,
        "deletion_codes": [],
    }
    return details, states


def _transaction_check(state_root: Path, now: datetime, deadline: float = float("inf")) -> dict:
    path = state_root / "run" / "markdown-transactions.sqlite3"
    details, states = _empty_transaction_details()
    kind, _ = _safe_kind(path, state_root)
    if kind == "missing":
        return _missing_transaction_database(path, state_root, details, deadline)
    if kind != "regular":
        return _unreadable_transactions(details, "Transaction database is unsafe.")
    try:
        incomplete = _scan_transaction_database(
            path, state_root, now, deadline, details, states
        )
    except (OSError, sqlite3.Error, TimeoutError, ValueError):
        return _unreadable_transactions(details, "Transaction state is unreadable.")
    if incomplete is not None:
        return incomplete
    return _transaction_result(details, states)


_QUEUE_COUNT_QUERIES = {
    "source_failures": "SELECT 1 FROM source_failures LIMIT ?",
    "source_fences": "SELECT 1 FROM source_fences LIMIT ?",
}


class _TaskVerdict(NamedTuple):
    unknown_state: bool
    corrupt: bool


class _QueueScan(NamedTuple):
    result: dict | None
    unknown_state: bool
    corrupt_metadata: bool


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
    marker = state_root / "run" / "queue-migrated-v2"
    marker_kind = _safe_kind(marker, state_root)[0]
    details["migration"] = "complete" if marker_kind == "regular" else "pending"
    if marker_kind not in {"missing", "regular"}:
        details["read_error"] = True
        details["deletion_codes"].append("queue_migration_state_unknown")
    if marker_kind == "regular" and details["legacy_retained"]:
        details["migration"] = "conflict"


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
    if state == "dead":
        return blocked_capability is None
    return False


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
    expires = _parse_utc(row["lease_expires_at"]) or datetime.min.replace(
        tzinfo=timezone.utc
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
    if _task_lease_live(row, row_columns, state, now):
        details["live_workers"] += 1
    return _TaskVerdict(False, not matches)


def _scan_queue_tasks(
    rows: list[sqlite3.Row],
    *,
    now: datetime,
    deadline: float,
    details: dict,
    states: dict[str, int],
) -> tuple[bool, bool, set[str], dict[str, object]]:
    codes: set[str] = set()
    capabilities: set[str] = set()
    references: set[str] = set()
    result_hashes: dict[str, object] = {}
    unknown_state = False
    corrupt_metadata = False
    for row in rows:
        if _deadline_reached(deadline):
            raise TimeoutError("queue check deadline")
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
    return unknown_state, corrupt_metadata, references, result_hashes


def _bounded_task_rows(database: sqlite3.Connection, details: dict) -> list[sqlite3.Row]:
    rows = database.execute(
        "SELECT * FROM tasks LIMIT ?", (MAX_OPERATIONAL_ROWS + 1,)
    ).fetchall()
    if len(rows) <= MAX_OPERATIONAL_ROWS:
        return rows
    details["codes"].append("queue_scan_truncated")
    details["deletion_codes"].append("queue_state_unknown")
    return rows[:MAX_OPERATIONAL_ROWS]


def _append_queue_scan_codes(
    details: dict, unknown_state: bool, corrupt_metadata: bool, rows: list
) -> None:
    if unknown_state:
        details["deletion_codes"].append("queue_state_unknown")
    if corrupt_metadata:
        details["deletion_codes"].append("queue_state_corrupt")
    if rows:
        details["deletion_codes"].append("queue_task_retained")


def _count_queue_rows(
    database: sqlite3.Connection,
    tables: set[str],
    details: dict,
    *,
    table: str,
    retained_code: str,
    unknown_code: str,
) -> None:
    if table not in tables:
        return
    rows = database.execute(
        _QUEUE_COUNT_QUERIES[table], (MAX_OPERATIONAL_ROWS + 1,)
    ).fetchall()
    details[table] = len(rows)
    if rows:
        details["deletion_codes"].append(retained_code)
    if len(rows) > MAX_OPERATIONAL_ROWS:
        details["deletion_codes"].append(unknown_code)


def _count_owner_role(row: sqlite3.Row, details: dict) -> None:
    if row["role"] == "worker":
        details["live_workers"] += 1
    if row["role"] == "migration":
        details["live_migrations"] += 1


def _count_one_queue_owner(row: sqlite3.Row, details: dict, now: datetime) -> None:
    if row["token"] is None:
        return
    if not _owner_row_known(row, pid_column="pid"):
        details["deletion_codes"].append("queue_owner_state_unknown")
        return
    if not _live_owner(row, now, pid_column="pid"):
        return
    _count_owner_role(row, details)


def _count_queue_ownership(
    database: sqlite3.Connection, tables: set[str], details: dict, now: datetime
) -> None:
    if "queue_ownership" not in tables:
        return
    rows = database.execute(
        "SELECT * FROM queue_ownership LIMIT ?", (MAX_OPERATIONAL_ROWS + 1,)
    ).fetchall()
    if len(rows) > MAX_OPERATIONAL_ROWS:
        details["deletion_codes"].append("queue_owner_state_unknown")
    for row in rows[:MAX_OPERATIONAL_ROWS]:
        _count_one_queue_owner(row, details, now)


def _count_queue_side_tables(
    database: sqlite3.Connection, tables: set[str], details: dict, now: datetime
) -> None:
    _count_queue_rows(
        database,
        tables,
        details,
        table="source_failures",
        retained_code="queue_source_failure_retained",
        unknown_code="queue_source_failure_state_unknown",
    )
    _count_queue_rows(
        database,
        tables,
        details,
        table="source_fences",
        retained_code="queue_source_fence_retained",
        unknown_code="queue_source_fence_state_unknown",
    )
    _count_queue_ownership(database, tables, details, now)


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
) -> None:
    for reference in references:
        raw = _queue_result_bytes(state_root, reference)
        expected = result_hashes.get(reference)
        if raw is None or not isinstance(expected, str):
            details["results_invalid"] += 1
            continue
        if hashlib.sha256(raw).hexdigest() != expected:
            details["results_invalid"] += 1


def _scan_queue_database(
    path: Path,
    state_root: Path,
    now: datetime,
    deadline: float,
    details: dict,
    states: dict[str, int],
) -> _QueueScan:
    with _readonly_database(path, state_root, deadline=deadline) as database:
        if _deadline_reached(deadline):
            raise TimeoutError("queue check deadline")
        tables = _tables(database, deadline)
        if "tasks" not in tables:
            raise sqlite3.DatabaseError("tasks table missing")
        task_columns = _columns(database, "tasks", deadline)
        if not {"state", "error_code", "blocked_capability"}.issubset(task_columns):
            details["codes"].append("queue_metadata_missing")
            details["deletion_codes"].append("queue_state_corrupt")
            return _QueueScan(
                _result("queue", "error", "Queue task metadata is incomplete.", details),
                False,
                False,
            )
        rows = _bounded_task_rows(database, details)
        unknown_state, corrupt_metadata, references, result_hashes = _scan_queue_tasks(
            rows, now=now, deadline=deadline, details=details, states=states
        )
        _append_queue_scan_codes(details, unknown_state, corrupt_metadata, rows)
        _count_queue_side_tables(database, tables, details, now)
        _validate_queue_results(state_root, references, result_hashes, details)
        return _QueueScan(None, unknown_state, corrupt_metadata)


def _queue_error_state(
    details: dict, unknown_state: bool, corrupt_metadata: bool
) -> bool:
    if unknown_state or corrupt_metadata:
        return True
    return bool(details["results_invalid"]) or details["migration"] == "conflict"


def _queue_pending_work(states: dict[str, int], details: dict) -> bool:
    if states["ready"] or states["leased"] or states["blocked"]:
        return True
    return details["migration"] == "pending"


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


def _queue_v2_check(state_root: Path, now: datetime, deadline: float) -> dict:
    path = state_root / "run" / "queue.sqlite3"
    details, states = _empty_queue_details()
    details.update(_queue_artifact_state(state_root, deadline))
    _record_queue_migration(state_root, details)
    try:
        scan = _scan_queue_database(path, state_root, now, deadline, details, states)
    except (OSError, PermissionError, sqlite3.Error, TimeoutError, ValueError):
        details["read_error"] = True
        details["deletion_codes"].append("queue_state_unreadable")
        return _result("queue", "error", "Queue state is unreadable.", details)
    if scan.result is not None:
        return scan.result
    if time.monotonic() >= deadline:
        details["budget_exhausted"] = True
    status = _queue_status(states, details, scan.unknown_state, scan.corrupt_metadata)
    _append_queue_deletion_codes(details)
    message = "Queue state is healthy."
    if status != "ok":
        message = "Queue state requires operator attention."
    return _result("queue", status, message, details)


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
    return any(_safe_kind(item, state_root)[0] != "regular" for item in entries)


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
    if _safe_kind(month, root)[0] != "directory":
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
    if truncated or len(months) > 120:
        details["codes"].append("archive_scan_truncated")
        details["deletion_codes"].append("archive_state_unknown")
        return months[:120]
    return months


def _is_bag_directory(item: Path, root: Path) -> bool:
    return _safe_kind(item, root)[0] == "directory" and item.name.startswith("bag-")


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
    manifest, problem = _read_bounded_json(
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
        if _deadline_reached(deadline):
            raise TimeoutError("archive check deadline")
        key = _archive_manifest_key(bag, archive, details)
        if key is None:
            continue
        if key in seen:
            details["duplicates"] += 1
        seen.add(key)
        bag_paths.add(bag.relative_to(root).as_posix())
    return bag_paths


def _bag_path_entry(item: object) -> bool:
    return isinstance(item, dict) and isinstance(item.get("bag_path"), str)


def _index_validity(index: dict, bag_paths: set[str]) -> str:
    indexed = index.get("bags", [])
    indexed_paths = {
        str(item.get("bag_path")) for item in indexed if _bag_path_entry(item)
    }
    if index.get("schema_version") != "archive-index/v1":
        return "invalid"
    if indexed_paths != bag_paths or len(indexed_paths) != len(indexed):
        return "invalid"
    return "valid"


def _archive_index_state(
    archive: Path, root: Path, bag_paths: set[str], deadline: float
) -> str:
    index_path = archive / "archive-index.json"
    index_kind = _safe_kind(index_path, root)[0]
    if index_kind == "missing":
        return "missing"
    if index_kind != "regular":
        return "invalid"
    index, problem = _read_bounded_json(
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
    return _result("archives", status, message, details)


def _archive_check(root: Path, state_root: Path, deadline: float = float("inf")) -> dict:
    archive = _archive_path(root)
    details = _empty_archive_details()
    _record_quarantine_state(state_root, deadline, details)
    archive_kind = _safe_kind(archive, root)[0]
    if archive_kind == "missing":
        return _result("archives", "ok", "No archive exists.", details)
    if archive_kind != "directory":
        details["read_error"] = True
        details["deletion_codes"].append("archive_state_unreadable")
        return _result("archives", "error", "Archive root is unsafe.", details)
    try:
        _scan_archive(archive, root, deadline, details)
    except (OSError, TimeoutError):
        details["codes"].append("archive_unreadable")
        details["read_error"] = True
        details["deletion_codes"].append("archive_state_unreadable")
    return _archive_result(details)


def _count_claims(database: sqlite3.Connection, details: dict) -> None:
    details["claims"] = len(
        database.execute(
            "SELECT 1 FROM claim LIMIT ?", (MAX_OPERATIONAL_ROWS + 1,)
        ).fetchall()
    )
    rows = database.execute(
        "SELECT code FROM claim_index_diagnostic ORDER BY code LIMIT ?",
        (MAX_OPERATIONAL_ROWS + 1,),
    ).fetchall()
    details["diagnostics"] = min(len(rows), MAX_OPERATIONAL_ROWS)
    details["codes"] = sorted({str(row[0]) for row in rows})
    if len(rows) > MAX_OPERATIONAL_ROWS or details["claims"] > MAX_OPERATIONAL_ROWS:
        details["codes"].append("claim_scan_truncated")


def _claim_status(details: dict) -> str:
    if details["index"] == "invalid":
        return "error"
    if details["diagnostics"]:
        return "degraded"
    return "ok"


def _claim_result(details: dict) -> dict:
    status = _claim_status(details)
    message = "Claim index is healthy."
    if status != "ok":
        message = "Claim index requires operator attention."
    return _result("claims", status, message, details)


def _claim_check(root: Path, state_root: Path, deadline: float = float("inf")) -> dict:
    path = state_root / "cache" / "claims.sqlite3"
    details = {
        "index": "missing",
        "claims": 0,
        "diagnostics": 0,
        "codes": [],
        "read_error": False,
        "deletion_codes": [],
    }
    kind = _safe_kind(path, state_root)[0]
    if kind == "missing":
        return _result("claims", "degraded", "Claim index is missing.", details)
    if kind != "regular":
        details.update(index="invalid", read_error=True)
        return _result("claims", "error", "Claim index is unsafe.", details)
    try:
        from claims import ClaimIndex

        with _readonly_database(path, state_root, deadline=deadline) as database:
            if _deadline_reached(deadline):
                raise TimeoutError("claim check deadline")
            compatible = ClaimIndex._schema_compatible(database)  # noqa: SLF001
            details["index"] = "valid" if compatible else "invalid"
            if compatible:
                _count_claims(database, details)
    except (OSError, PermissionError, sqlite3.Error, TimeoutError, ValueError):
        details["index"] = "invalid"
        details["read_error"] = True
    return _claim_result(details)


def _filesystem_check(state_root: Path, deadline: float = float("inf")) -> dict:
    if _deadline_reached(deadline):
        return _result(
            "filesystem",
            "error",
            "Filesystem check exceeded its deadline.",
            {
                "local": False,
                "locking": "unknown",
                "budget_exhausted": True,
                "read_error": True,
            },
        )
    try:
        network = reliable_memory._known_network_path(state_root)
    except (OSError, RuntimeError, ValueError):
        return _result(
            "filesystem",
            "degraded",
            "Runtime filesystem type could not be determined.",
            {"local": False, "locking": "unknown", "read_error": True},
        )
    if network:
        return _result(
            "filesystem",
            "error",
            "Runtime must use a local filesystem.",
            {"local": False, "locking": "unsupported"},
        )
    if not state_root.is_dir():
        return _result(
            "filesystem",
            "degraded",
            "Runtime filesystem locking cannot be probed until the state root exists.",
            {"local": True, "locking": "unknown"},
        )
    probe_deadline = min(deadline, time.monotonic() + FILESYSTEM_PROBE_SECONDS)
    try:
        locking = reliable_memory._sqlite_lock_probe(state_root, deadline=probe_deadline)
    except (OSError, RuntimeError, sqlite3.Error):
        locking = None
    details = {
        "local": True,
        "locking": "supported"
        if locking is True
        else "unsupported"
        if locking is False
        else "unknown",
    }
    status = "ok" if locking is True else "error" if locking is False else "degraded"
    return _result(
        "filesystem",
        status,
        "Runtime filesystem supports local locking."
        if locking is True
        else "Runtime filesystem locking is broken."
        if locking is False
        else "Runtime filesystem locking probe is unavailable.",
        details,
    )


def _deletion_snapshot(codes: list[str]) -> dict[str, object]:
    blockers = [{"code": code} for code in sorted(set(codes))]
    return {
        "schema_version": "run-deletion-snapshot/v1",
        "quiescent": not blockers,
        "permit": False,
        "offline_action_required": True,
        "blockers": blockers,
    }


def _derived_deletion_codes(check: dict) -> list[str]:
    """Deletion codes a check contributes, including its own unreadable state."""
    details = check.get("details", {})
    codes = [str(code) for code in details.get("deletion_codes", [])]
    if codes or not details.get("read_error"):
        return codes
    return [f"{check['id']}_state_unreadable"]


def _snapshot_deletion_codes(
    root_path: Path,
    state_path: Path,
    now: datetime,
    snapshot_deadline: float,
    owner: object,
    validate_reliability_v3_runtime,
) -> list[str]:
    codes = list(
        validate_reliability_v3_runtime(
            root=root_path,
            state_root=state_path,
            now=now,
            deadline=snapshot_deadline,
            excluded_owner=owner,
        )
    )
    for check in (
        _archive_check(root_path, state_path, snapshot_deadline),
        _lsp_runtime_check(state_path, now, deadline=snapshot_deadline),
    ):
        codes.extend(_derived_deletion_codes(check))
    return codes


def _observed_deletion_codes(
    root_path: Path,
    state_path: Path,
    now: datetime,
    snapshot_deadline: float,
    owner: object,
    validate_reliability_v3_runtime,
) -> list[str]:
    try:
        codes = _snapshot_deletion_codes(
            root_path,
            state_path,
            now,
            snapshot_deadline,
            owner,
            validate_reliability_v3_runtime,
        )
    except (OSError, PermissionError, sqlite3.Error, TimeoutError, ValueError):
        codes = ["run_deletion_state_unknown"]
    if _deadline_reached(snapshot_deadline):
        codes.append("run_deletion_state_unknown")
    return codes


def _deletion_root(root: Path | None, state_path: Path) -> Path:
    if root is None:
        return state_path
    return Path(root)


def _run_deletion_check(
    state_root: Path,
    now: datetime,
    *,
    root: Path | None = None,
    deadline: float = float("inf"),
    collected: dict[str, dict] | None = None,
) -> dict:
    """Return an immediate, non-permitting observation of adopted runtime state."""
    del collected
    from installed_memory_repair import (
        ReliabilityV3ValidationError,
        require_reliability_v3_adopted,
        validate_reliability_v3_runtime,
    )
    from operational_ownership import OperationalOwnershipError, OwnershipRegistry

    state_path = Path(state_root)
    root_path = _deletion_root(root, state_path)
    try:
        require_reliability_v3_adopted(root=root_path, state_root=state_path)
    except ReliabilityV3ValidationError as exc:
        return _deletion_snapshot([exc.code])

    snapshot_deadline = min(deadline, time.monotonic() + 20.0)
    if _deadline_reached(snapshot_deadline):
        return _deletion_snapshot(["run_deletion_state_unknown"])
    registry = OwnershipRegistry._from_adopted_database(  # noqa: SLF001
        state_path,
        state_path / "run" / "markdown-transactions-v3.sqlite3",
    )
    try:
        owner = registry.acquire("runtime-deletion-check", scope="global")
    except (OperationalOwnershipError, OSError, sqlite3.Error, ValueError) as exc:
        code = getattr(exc, "code", "runtime_deletion_check_unavailable")
        return _deletion_snapshot([str(code)])

    codes: list[str] = []
    try:
        codes = _observed_deletion_codes(
            root_path,
            state_path,
            now,
            snapshot_deadline,
            owner,
            validate_reliability_v3_runtime,
        )
    finally:
        try:
            registry.release(owner)
        except (OperationalOwnershipError, OSError, sqlite3.Error, ValueError):
            codes.append("runtime_deletion_check_release_failed")
    return _deletion_snapshot(codes)


LSP_FAILURE_RETENTION = timedelta(days=7)
MAX_LSP_OWNER_ROWS = 128
_LSP_OWNER_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_LSP_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z\Z")
_LSP_OWNER_FIELDS = {
    "command_basename",
    "generation_nonce",
    "owner_nonce",
    "owner_pid",
    "started_at",
    "state",
}
_LSP_LEASE_FIELDS = {
    "expires_at",
    "generation_nonce",
    "heartbeat_at",
    "manager_pid",
    "owner_nonce",
    "schema_version",
    "server_pid",
    "state",
}
_LSP_FAILURE_FIELDS = {"code", "generation_nonce", "owner_nonce", "timestamp"}
_LSP_OWNER_ENTRY_NAMES = {"cancellation", "failure.json", "lease.json", "owner.json"}
_LSP_RECORD_NAMES = {"failure.json", "lease.json", "owner.json"}


def _lsp_positive_pid(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _parse_lsp_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or _LSP_TIMESTAMP.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    if parsed.tzinfo != timezone.utc or parsed.isoformat().replace("+00:00", "Z") != value:
        return None
    return parsed


def _valid_lsp_owner(record: dict[str, Any], owner_nonce: str) -> bool:
    command = record.get("command_basename")
    return (
        set(record) == _LSP_OWNER_FIELDS
        and isinstance(command, str)
        and 0 < len(command) <= 255
        and not any(character in command for character in "/\\\x00\r\n")
        and record.get("owner_nonce") == owner_nonce
        and isinstance(record.get("generation_nonce"), str)
        and _LSP_OWNER_NONCE.fullmatch(record["generation_nonce"]) is not None
        and _lsp_positive_pid(record.get("owner_pid"))
        and _parse_lsp_timestamp(record.get("started_at")) is not None
        and record.get("state") == "process_running"
    )


def _lsp_schema_version_one(record: dict[str, Any]) -> bool:
    version = record.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        return False
    return version == 1


def _lsp_generation_nonce_valid(record: dict[str, Any]) -> bool:
    nonce = record.get("generation_nonce")
    if not isinstance(nonce, str):
        return False
    return _LSP_OWNER_NONCE.fullmatch(nonce) is not None


def _lsp_lease_window_valid(record: dict[str, Any]) -> bool:
    heartbeat = _parse_lsp_timestamp(record.get("heartbeat_at"))
    expires = _parse_lsp_timestamp(record.get("expires_at"))
    if heartbeat is None or expires is None:
        return False
    return heartbeat < expires


def _lsp_lease_identity_valid(record: dict[str, Any], owner_nonce: str) -> bool:
    if set(record) != _LSP_LEASE_FIELDS or not _lsp_schema_version_one(record):
        return False
    if record.get("owner_nonce") != owner_nonce:
        return False
    return _lsp_generation_nonce_valid(record)


def _valid_lsp_lease(record: dict[str, Any], owner_nonce: str) -> bool:
    if not _lsp_lease_identity_valid(record, owner_nonce):
        return False
    if not _lsp_positive_pid(record.get("manager_pid")):
        return False
    if not _lsp_positive_pid(record.get("server_pid")):
        return False
    if record.get("state") != "live":
        return False
    return _lsp_lease_window_valid(record)


def _valid_lsp_failure(record: dict[str, Any], owner_nonce: str) -> bool:
    fields = set(record)
    code = record.get("code")
    return (
        fields in (_LSP_FAILURE_FIELDS, _LSP_FAILURE_FIELDS | {"server_pid"})
        and isinstance(code, str)
        and re.fullmatch(r"[a-z0-9_]{1,64}", code) is not None
        and record.get("owner_nonce") == owner_nonce
        and isinstance(record.get("generation_nonce"), str)
        and _LSP_OWNER_NONCE.fullmatch(record["generation_nonce"]) is not None
        and _parse_lsp_timestamp(record.get("timestamp")) is not None
        and ("server_pid" not in record or _lsp_positive_pid(record.get("server_pid")))
    )


def _pyright_check(
    root: Path,
    state_root: Path,
    *,
    deadline: float,
) -> dict:
    """Report pinned Pyright identity without network access or mutation."""
    from pyright_profile import discover_pyright
    from repository_scope import resolve_repository_scope

    codes: list[str] = []
    details: dict[str, Any] = {
        "status": "qualified",
        "source": None,
        "version": None,
        "node_major": None,
        "node_version": None,
        "package_sha256": None,
        "executable_sha256": None,
        "initialization_options_sha256": None,
        "configuration_sha256": None,
        "qualified": False,
        "codes": codes,
    }
    api_deadline = None if math.isinf(deadline) else deadline
    try:
        scope = resolve_repository_scope(root, deadline=api_deadline)
        identity = discover_pyright(
            scope,
            state_root=state_root,
            deadline=api_deadline,
        )
    except TimeoutError:
        details["status"] = "timeout"
        codes.append("pyright_timeout")
        return _result(
            "pyright",
            "degraded",
            "Pyright discovery did not complete before the deadline.",
            details,
        )
    except Exception:  # noqa: BLE001
        details["status"] = "unsafe"
        codes.append("pyright_unsafe")
        return _result(
            "pyright",
            "degraded",
            "Pyright discovery could not safely inspect the repository.",
            details,
        )
    details.update(
        {
            "status": identity.status,
            "source": identity.source,
            "version": identity.version,
            "node_major": identity.node_major,
            "node_version": identity.node_version,
            "package_sha256": identity.package_sha256,
            "executable_sha256": identity.executable_sha256,
            "initialization_options_sha256": identity.initialization_options_sha256,
            "configuration_sha256": identity.configuration_sha256,
            "qualified": identity.qualified,
        }
    )
    details["executable_sha256_present"] = identity.executable_sha256 is not None
    if not identity.qualified:
        if identity.status == "missing":
            codes.append("pyright_missing")
        else:
            details["status"] = "degraded"
            for code in identity.degradation_codes:
                if code not in codes:
                    codes.append(code)
            if identity.version != "1.1.411" and "pyright_version_mismatch" not in codes:
                codes.append("pyright_version_mismatch")
        details["recommended_action"] = (
            "uv run python scripts/install_pyright.py --state-root <state-root>"
        )
        return _result(
            "pyright",
            "degraded",
            "Pyright identity is degraded or mismatched.",
            details,
        )
    return _result("pyright", "ok", "Pyright identity is qualified.", details)


def _navigation_optional_check(
    state_root: Path,
    check_id: str,
    run_check: Callable[[], dict],
) -> dict:
    """Skip navigation diagnostics when the feature is not configured."""
    lsp_root = state_root / "run" / "lsp"
    managed = state_root / "cache" / "code-tools" / "pyright"
    if not lsp_root.exists() and not managed.exists():
        return _result(
            check_id,
            "skipped",
            "Code navigation is not configured.",
            {"configured": False},
        )
    return run_check()


_LSP_RECORD_BYTES = 64 * 1024
_LSP_READ_CHUNK_BYTES = 4096
_LSP_JSON_MAX_DEPTH = 32
_LspOwnerSnapshot = tuple[
    str,
    frozenset[str],
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]


def _require_lsp_deadline(deadline: float) -> None:
    if _deadline_reached(deadline):
        raise TimeoutError("LSP runtime scan deadline reached")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key, value in pairs:
        if key in record:
            raise ValueError(f"duplicate JSON key: {key}")
        record[key] = value
    return record


def _require_lsp_json_depth(text: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > _LSP_JSON_MAX_DEPTH:
                raise ValueError("LSP runtime record is too deeply nested")
        elif character in "]}":
            depth -= 1


def _decode_lsp_record(payload: bytes) -> dict[str, Any]:
    text = payload.decode("utf-8", errors="strict")
    _require_lsp_json_depth(text)
    try:
        record = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
        )
    except RecursionError as exc:
        raise ValueError("LSP runtime record is too deeply nested") from exc
    if not isinstance(record, dict):
        raise ValueError("LSP runtime record must be a JSON object")
    return record


def _lsp_posix_directory_flags() -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise OSError("POSIX no-follow directory handles are unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_posix_lsp_root(state_root: Path, deadline: float) -> int:
    state_root = Path(os.path.abspath(state_root))
    anchor = Path(state_root.anchor)
    if not state_root.is_absolute() or not anchor.anchor:
        raise ValueError("LSP state root must be an absolute local path")
    components = (*state_root.relative_to(anchor).parts, "run", "lsp")
    flags = _lsp_posix_directory_flags()
    current: int | None = None
    try:
        _require_lsp_deadline(deadline)
        current = os.open(anchor, flags)
        _require_lsp_deadline(deadline)
        if not stat.S_ISDIR(os.fstat(current).st_mode):
            raise PermissionError("LSP path anchor is not a directory")
        _require_lsp_deadline(deadline)
        for component in components:
            _require_lsp_deadline(deadline)
            opened = os.open(component, flags, dir_fd=current)
            try:
                _require_lsp_deadline(deadline)
                if not stat.S_ISDIR(os.fstat(opened).st_mode):
                    raise PermissionError("LSP path component is not a directory")
                _require_lsp_deadline(deadline)
            except BaseException:
                os.close(opened)
                raise
            previous = current
            current = opened
            os.close(previous)
        if current is None:
            raise OSError("LSP root descriptor was not retained")
        return current
    except BaseException:
        if current is not None:
            os.close(current)
        raise


def _list_posix_lsp_names(
    directory_fd: int,
    *,
    observed_limit: int,
    deadline: float,
) -> tuple[list[str], bool]:
    names: list[str] = []
    _require_lsp_deadline(deadline)
    iterator = os.scandir(directory_fd)
    _require_lsp_deadline(deadline)
    with iterator:
        while len(names) < observed_limit:
            _require_lsp_deadline(deadline)
            try:
                entry = next(iterator)
            except StopIteration:
                _require_lsp_deadline(deadline)
                return names, False
            _require_lsp_deadline(deadline)
            names.append(entry.name)
    return names, len(names) == observed_limit


def _open_posix_lsp_directory(
    parent_fd: int,
    name: str,
    deadline: float,
) -> int:
    _require_lsp_deadline(deadline)
    expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    _require_lsp_deadline(deadline)
    if stat.S_ISLNK(expected.st_mode) or not stat.S_ISDIR(expected.st_mode):
        raise PermissionError("LSP runtime member is not a real directory")
    opened = os.open(
        name,
        _lsp_posix_directory_flags(),
        dir_fd=parent_fd,
    )
    try:
        _require_lsp_deadline(deadline)
        current = os.fstat(opened)
        _require_lsp_deadline(deadline)
        if not stat.S_ISDIR(current.st_mode) or not os.path.samestat(expected, current):
            raise PermissionError("LSP runtime directory changed before open")
        return opened
    except BaseException:
        os.close(opened)
        raise


def _read_posix_lsp_record(
    owner_fd: int,
    name: str,
    deadline: float,
) -> dict[str, Any]:
    _require_lsp_deadline(deadline)
    expected = os.stat(name, dir_fd=owner_fd, follow_symlinks=False)
    _require_lsp_deadline(deadline)
    if (
        stat.S_ISLNK(expected.st_mode)
        or not stat.S_ISREG(expected.st_mode)
        or expected.st_size > _LSP_RECORD_BYTES
    ):
        raise PermissionError("LSP runtime record is unsafe or oversized")
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        dir_fd=owner_fd,
    )
    try:
        _require_lsp_deadline(deadline)
        opened = os.fstat(descriptor)
        _require_lsp_deadline(deadline)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(expected, opened):
            raise PermissionError("LSP runtime record changed before open")
        identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        chunks: list[bytes] = []
        total = 0
        while total <= _LSP_RECORD_BYTES:
            _require_lsp_deadline(deadline)
            chunk = os.read(
                descriptor,
                min(_LSP_READ_CHUNK_BYTES, _LSP_RECORD_BYTES + 1 - total),
            )
            _require_lsp_deadline(deadline)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > _LSP_RECORD_BYTES:
            raise ValueError("LSP runtime record exceeds its byte bound")
        _require_lsp_deadline(deadline)
        after = os.fstat(descriptor)
        _require_lsp_deadline(deadline)
        if total != opened.st_size or identity != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise PermissionError("LSP runtime record changed during read")
        return _decode_lsp_record(b"".join(chunks))
    finally:
        os.close(descriptor)


class _PosixOwnerReading(NamedTuple):
    snapshot: tuple
    unreadable: bool
    stop: bool


def _posix_owner_snapshot(
    owner_name: str, present: frozenset[str], records: dict
) -> tuple:
    return (
        owner_name,
        present,
        records["owner.json"],
        records["lease.json"],
        records["failure.json"],
    )


def _read_posix_child(
    owner_fd: int, child_name: str, deadline: float, records: dict
) -> bool:
    """True when this child could not be read."""
    if child_name not in _LSP_OWNER_ENTRY_NAMES:
        return True
    if child_name == "cancellation":
        os.close(_open_posix_lsp_directory(owner_fd, child_name, deadline))
        return False
    try:
        records[child_name] = _read_posix_lsp_record(owner_fd, child_name, deadline)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return True
    return False


def _read_posix_owner_children(
    owner_fd: int, deadline: float, records: dict
) -> tuple[frozenset[str], bool]:
    child_names, truncated = _list_posix_lsp_names(
        owner_fd,
        observed_limit=len(_LSP_OWNER_ENTRY_NAMES) + 1,
        deadline=deadline,
    )
    bounded_names = child_names[: len(_LSP_OWNER_ENTRY_NAMES)]
    present = frozenset(bounded_names)
    unreadable = truncated or "cancellation" not in present
    for child_name in bounded_names:
        unreadable = _read_posix_child(owner_fd, child_name, deadline, records) or unreadable
    return present, unreadable


def _read_posix_owner(
    lsp_fd: int, owner_name: str, deadline: float
) -> _PosixOwnerReading:
    records: dict[str, dict[str, Any] | None] = {
        name: None for name in _LSP_RECORD_NAMES
    }
    present: frozenset[str] = frozenset()
    owner_fd: int | None = None
    unreadable = False
    stop = False
    try:
        owner_fd = _open_posix_lsp_directory(lsp_fd, owner_name, deadline)
        present, unreadable = _read_posix_owner_children(owner_fd, deadline, records)
    except TimeoutError:
        unreadable = True
        stop = True
    except (OSError, ValueError):
        unreadable = True
    finally:
        if owner_fd is not None:
            os.close(owner_fd)
    snapshot = _posix_owner_snapshot(owner_name, present, records)
    return _PosixOwnerReading(snapshot, unreadable, stop)


def _scan_posix_owners(
    lsp_fd: int, owner_names: list[str], deadline: float
) -> tuple[list, bool]:
    snapshots: list = []
    unreadable = False
    for owner_name in sorted(owner_names[:MAX_LSP_OWNER_ROWS]):
        if _LSP_OWNER_NONCE.fullmatch(owner_name) is None:
            unreadable = True
            continue
        reading = _read_posix_owner(lsp_fd, owner_name, deadline)
        unreadable = unreadable or reading.unreadable
        snapshots.append(reading.snapshot)
        if reading.stop:
            return snapshots, True
    return snapshots, unreadable


def _posix_lsp_snapshots(lsp_fd: int, deadline: float) -> tuple[list, bool, bool]:
    try:
        owner_names, truncated = _list_posix_lsp_names(
            lsp_fd,
            observed_limit=MAX_LSP_OWNER_ROWS + 1,
            deadline=deadline,
        )
    except (OSError, ValueError, TimeoutError):
        return [], True, False
    snapshots: list = []
    try:
        snapshots, unreadable = _scan_posix_owners(lsp_fd, owner_names, deadline)
        _require_lsp_deadline(deadline)
    except TimeoutError:
        return snapshots, True, False
    return snapshots, unreadable or truncated, False


def _snapshot_posix_lsp(
    state_root: Path,
    deadline: float,
) -> tuple[list[_LspOwnerSnapshot], bool, bool]:
    try:
        lsp_fd = _open_posix_lsp_root(state_root, deadline)
    except FileNotFoundError:
        if _deadline_reached(deadline):
            return [], True, False
        return [], False, True
    except (OSError, ValueError, TimeoutError):
        return [], True, False
    try:
        return _posix_lsp_snapshots(lsp_fd, deadline)
    finally:
        os.close(lsp_fd)


def _windows_lsp_identity(workspace, handle: int, *, directory: bool) -> bytes:
    _volume, file_id, _kind = workspace.identity(handle, directory=directory)
    if not isinstance(file_id, bytes) or not any(file_id):
        raise OSError("Windows LSP identity is unavailable")
    return file_id


def _read_windows_lsp_record(
    workspace,
    owner_handle: int,
    entry,
    deadline: float,
) -> dict[str, Any]:
    if entry.kind != "file" or entry.size > _LSP_RECORD_BYTES:
        raise PermissionError("Windows LSP record is unsafe or oversized")
    _require_lsp_deadline(deadline)
    handle = workspace.open_file(owner_handle, entry.name)
    try:
        _require_lsp_deadline(deadline)
        if _windows_lsp_identity(workspace, handle, directory=False) != entry.file_id:
            raise PermissionError("Windows LSP record changed before open")
        _require_lsp_deadline(deadline)
        size = workspace.file_size(handle)
        _require_lsp_deadline(deadline)
        if size != entry.size or size > _LSP_RECORD_BYTES:
            raise PermissionError("Windows LSP record size changed before read")
        chunks: list[bytes] = []
        total = 0
        iterator = iter(
            workspace.read_chunks(
                handle,
                chunk_bytes=_LSP_READ_CHUNK_BYTES,
                max_bytes=_LSP_RECORD_BYTES,
            )
        )
        while True:
            _require_lsp_deadline(deadline)
            try:
                chunk = next(iterator)
            except StopIteration:
                _require_lsp_deadline(deadline)
                break
            _require_lsp_deadline(deadline)
            chunks.append(chunk)
            total += len(chunk)
        _require_lsp_deadline(deadline)
        after_size = workspace.file_size(handle)
        _require_lsp_deadline(deadline)
        if total != size or after_size != size:
            raise PermissionError("Windows LSP record changed during read")
        return _decode_lsp_record(b"".join(chunks))
    finally:
        workspace.close_handle(handle)


def _snapshot_windows_lsp(
    state_root: Path,
    deadline: float,
) -> tuple[list[_LspOwnerSnapshot], bool, bool]:
    import windows_workspace as workspace

    lsp_root = Path(os.path.abspath(state_root)) / "run" / "lsp"
    snapshots: list[_LspOwnerSnapshot] = []
    unreadable = False
    try:
        _require_lsp_deadline(deadline)
        lsp_handle = workspace.open_directory_path(lsp_root)
    except FileNotFoundError:
        if _deadline_reached(deadline):
            return [], True, False
        return [], False, True
    except (OSError, ValueError, RuntimeError, TimeoutError):
        return [], True, False
    try:
        try:
            _require_lsp_deadline(deadline)
            owner_entries = workspace.list_directory(
                lsp_handle,
                max_entries=MAX_LSP_OWNER_ROWS,
            )
            _require_lsp_deadline(deadline)
        except (OSError, ValueError, RuntimeError, TimeoutError):
            return [], True, False
        for owner_entry in owner_entries:
            if (
                owner_entry.kind != "directory"
                or _LSP_OWNER_NONCE.fullmatch(owner_entry.name) is None
            ):
                unreadable = True
                continue
            owner_handle: int | None = None
            present: frozenset[str] = frozenset()
            records: dict[str, dict[str, Any] | None] = {name: None for name in _LSP_RECORD_NAMES}
            try:
                _require_lsp_deadline(deadline)
                owner_handle = workspace.open_directory(lsp_handle, owner_entry.name)
                _require_lsp_deadline(deadline)
                if (
                    _windows_lsp_identity(workspace, owner_handle, directory=True)
                    != owner_entry.file_id
                ):
                    raise PermissionError("Windows LSP owner changed before open")
                _require_lsp_deadline(deadline)
                child_entries = workspace.list_directory(
                    owner_handle,
                    max_entries=len(_LSP_OWNER_ENTRY_NAMES),
                )
                _require_lsp_deadline(deadline)
                present = frozenset(entry.name for entry in child_entries)
                if "cancellation" not in present:
                    unreadable = True
                for child_entry in child_entries:
                    if child_entry.name not in _LSP_OWNER_ENTRY_NAMES:
                        unreadable = True
                        continue
                    if child_entry.name == "cancellation":
                        if child_entry.kind != "directory":
                            unreadable = True
                            continue
                        _require_lsp_deadline(deadline)
                        cancellation = workspace.open_directory(owner_handle, child_entry.name)
                        try:
                            _require_lsp_deadline(deadline)
                            if (
                                _windows_lsp_identity(workspace, cancellation, directory=True)
                                != child_entry.file_id
                            ):
                                raise PermissionError("Windows LSP cancellation directory changed")
                            _require_lsp_deadline(deadline)
                        finally:
                            workspace.close_handle(cancellation)
                        continue
                    try:
                        records[child_entry.name] = _read_windows_lsp_record(
                            workspace,
                            owner_handle,
                            child_entry,
                            deadline,
                        )
                    except (
                        OSError,
                        UnicodeError,
                        ValueError,
                        json.JSONDecodeError,
                        RuntimeError,
                    ):
                        unreadable = True
            except TimeoutError:
                unreadable = True
                snapshots.append(
                    (
                        owner_entry.name,
                        present,
                        records["owner.json"],
                        records["lease.json"],
                        records["failure.json"],
                    )
                )
                break
            except (OSError, ValueError, RuntimeError):
                unreadable = True
            finally:
                if owner_handle is not None:
                    workspace.close_handle(owner_handle)
            snapshots.append(
                (
                    owner_entry.name,
                    present,
                    records["owner.json"],
                    records["lease.json"],
                    records["failure.json"],
                )
            )
        _require_lsp_deadline(deadline)
        return snapshots, unreadable, False
    except TimeoutError:
        return snapshots, True, False
    finally:
        workspace.close_handle(lsp_handle)


def _snapshot_lsp_runtime(
    state_root: Path,
    deadline: float,
) -> tuple[list[_LspOwnerSnapshot], bool, bool]:
    if os.name == "posix":
        return _snapshot_posix_lsp(state_root, deadline)
    if os.name == "nt":
        return _snapshot_windows_lsp(state_root, deadline)
    return [], True, False


class _LspLiveness(NamedTuple):
    live: bool
    heartbeat_at: datetime | None
    unreadable: bool
    stop: bool


class _LspOwnerReading(NamedTuple):
    record: dict
    codes: list[str]
    unreadable: bool
    stop: bool


def _lsp_records_missing(child_names: set[str], owner: object, lease: object) -> bool:
    if "owner.json" not in child_names or owner is None:
        return True
    return "lease.json" in child_names and lease is None


def _validated_lsp_owner_record(
    owner: dict | None, entry_name: str, now: datetime
) -> tuple[dict | None, bool]:
    if owner is None:
        return None, False
    if not _valid_lsp_owner(owner, entry_name):
        return None, True
    started_at = _parse_lsp_timestamp(owner.get("started_at"))
    return owner, started_at is not None and started_at > now


def _validated_lsp_lease_record(
    lease: dict | None, entry_name: str
) -> tuple[dict | None, bool]:
    if lease is None:
        return None, False
    if not _valid_lsp_lease(lease, entry_name):
        return None, True
    return lease, False


def _validated_lsp_records(
    entry_name: str,
    child_names: set[str],
    owner: dict | None,
    lease: dict | None,
    now: datetime,
) -> tuple[dict | None, dict | None, bool]:
    missing = _lsp_records_missing(child_names, owner, lease)
    owner, owner_invalid = _validated_lsp_owner_record(owner, entry_name, now)
    lease, lease_invalid = _validated_lsp_lease_record(lease, entry_name)
    return owner, lease, missing or owner_invalid or lease_invalid


def _lsp_nonces_match(owner: dict, lease: dict, entry_name: str) -> bool:
    if owner.get("owner_nonce") != entry_name or lease.get("owner_nonce") != entry_name:
        return False
    return owner.get("generation_nonce") == lease.get("generation_nonce")


def _lsp_records_match(
    owner: dict,
    lease: dict,
    entry_name: str,
    now: datetime,
    heartbeat_at: datetime | None,
) -> bool:
    if not _lsp_nonces_match(owner, lease, entry_name):
        return False
    if owner.get("owner_pid") != lease.get("server_pid"):
        return False
    started_at = _parse_lsp_timestamp(owner.get("started_at"))
    if started_at is None or heartbeat_at is None:
        return False
    return started_at <= heartbeat_at <= now


def _valid_lsp_pid(pid: object) -> bool:
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 0


def _lease_still_live(
    matching: bool, expires_at: datetime | None, now: datetime, pids: tuple
) -> bool:
    if not matching or expires_at is None or expires_at <= now:
        return False
    return all(_valid_lsp_pid(pid) for pid in pids)


def _lsp_pid_states(pids: tuple, deadline: float) -> list[str]:
    states: list[str] = []
    for pid in pids:
        _require_lsp_deadline(deadline)
        try:
            pid_state = _lsp_pid_state(pid)
        finally:
            _require_lsp_deadline(deadline)
        states.append(pid_state)
    return states


def _lsp_pid_liveness(
    pids: tuple, heartbeat_at: datetime | None, deadline: float
) -> _LspLiveness:
    try:
        pid_states = _lsp_pid_states(pids, deadline)
    except TimeoutError:
        return _LspLiveness(False, heartbeat_at, True, _deadline_reached(deadline))
    except Exception:  # noqa: BLE001
        return _LspLiveness(False, heartbeat_at, True, False)
    live = all(state == "alive" for state in pid_states)
    return _LspLiveness(live, heartbeat_at, "unknown" in pid_states, False)


def _lsp_liveness(
    owner: dict | None,
    lease: dict | None,
    entry_name: str,
    now: datetime,
    deadline: float,
) -> _LspLiveness:
    """Whether this owner still holds live processes, and what that cost to learn."""
    if not isinstance(owner, dict) or not isinstance(lease, dict):
        return _LspLiveness(False, None, False, False)
    heartbeat_at = _parse_lsp_timestamp(lease.get("heartbeat_at"))
    matching = _lsp_records_match(owner, lease, entry_name, now, heartbeat_at)
    pids = (lease.get("manager_pid"), lease.get("server_pid"))
    expires_at = _parse_lsp_timestamp(lease.get("expires_at"))
    if not _lease_still_live(matching, expires_at, now, pids):
        return _LspLiveness(False, heartbeat_at, not matching, False)
    return _lsp_pid_liveness(pids, heartbeat_at, deadline)


def _failure_identity_mismatch(owner: dict, failure: dict) -> bool:
    if failure.get("generation_nonce") != owner.get("generation_nonce"):
        return True
    return "server_pid" in failure and failure.get("server_pid") != owner.get(
        "owner_pid"
    )


def _failure_time_invalid(owner: dict, failure: dict, now: datetime) -> bool:
    owner_started_at = _parse_lsp_timestamp(owner.get("started_at"))
    failed_at = _parse_lsp_timestamp(failure.get("timestamp"))
    if owner_started_at is None or failed_at is None:
        return True
    return not owner_started_at <= failed_at <= now


def _failure_contradicts_owner(
    owner: object, failure: object, now: datetime
) -> bool:
    if not isinstance(owner, dict) or not isinstance(failure, dict):
        return False
    return _failure_identity_mismatch(owner, failure) or _failure_time_invalid(
        owner, failure, now
    )


def _validated_failure_record(
    failure: dict | None, entry_name: str
) -> tuple[dict | None, bool]:
    if failure is None:
        return None, True
    if not _valid_lsp_failure(failure, entry_name):
        return None, True
    return failure, False


def _failure_age_days(failure: object, now: datetime) -> float | None:
    if not isinstance(failure, dict):
        return None
    failed_at = _parse_lsp_timestamp(failure.get("timestamp"))
    if failed_at is None:
        return None
    return (now - failed_at).total_seconds() / 86400.0


def _record_explicit_failure(
    record: dict,
    codes: list[str],
    failure: dict | None,
    owner: object,
    entry_name: str,
    now: datetime,
) -> bool:
    failure, unreadable = _validated_failure_record(failure, entry_name)
    if _failure_contradicts_owner(owner, failure, now):
        unreadable = True
    age_days = _failure_age_days(failure, now)
    record["failure_evidence"] = True
    record["failure_age_days"] = age_days
    retention_days = LSP_FAILURE_RETENTION.total_seconds() / 86400.0
    if age_days is None or age_days < retention_days:
        codes.append("lsp_failure_evidence_retained")
    return unreadable


def _crash_timestamp(owner: object, heartbeat_at: datetime | None):
    if heartbeat_at is not None:
        return heartbeat_at
    if not isinstance(owner, dict):
        return None
    return _parse_lsp_timestamp(owner.get("started_at"))


def _record_crash_evidence(
    record: dict,
    codes: list[str],
    owner: object,
    heartbeat_at: datetime | None,
    now: datetime,
) -> None:
    crash_at = _crash_timestamp(owner, heartbeat_at)
    if crash_at is not None:
        record["failure_evidence"] = True
        record["failure_age_days"] = (now - crash_at).total_seconds() / 86400.0
    crash_age = record["failure_age_days"]
    if crash_age is None or crash_age < 7:
        codes.append("lsp_failure_evidence_retained")


def _record_failure_evidence(
    record: dict,
    codes: list[str],
    child_names: set[str],
    failure: dict | None,
    owner: object,
    entry_name: str,
    now: datetime,
    heartbeat_at: datetime | None,
) -> bool:
    if "failure.json" in child_names:
        return _record_explicit_failure(record, codes, failure, owner, entry_name, now)
    if record["live"]:
        return False
    _record_crash_evidence(record, codes, owner, heartbeat_at, now)
    return False


def _read_lsp_owner(snapshot: tuple, now: datetime, deadline: float) -> _LspOwnerReading:
    entry_name, child_names, owner, lease, failure = snapshot
    record: dict[str, Any] = {
        "owner_nonce": entry_name,
        "live": False,
        "failure_evidence": False,
        "failure_age_days": None,
    }
    codes: list[str] = []
    owner, lease, unreadable = _validated_lsp_records(
        entry_name, child_names, owner, lease, now
    )
    liveness = _lsp_liveness(owner, lease, entry_name, now, deadline)
    record["live"] = liveness.live
    if liveness.live:
        codes.append("lsp_owner_live")
    failure_unreadable = _record_failure_evidence(
        record,
        codes,
        child_names,
        failure,
        owner,
        entry_name,
        now,
        liveness.heartbeat_at,
    )
    stop = liveness.stop or _deadline_reached(deadline)
    unreadable = unreadable or liveness.unreadable or failure_unreadable
    return _LspOwnerReading(record, codes, unreadable, stop)


def _scan_lsp_owners(
    snapshots: list[tuple], now: datetime, deadline: float
) -> tuple[list[dict], list[str], bool]:
    owners: list[dict] = []
    codes: list[str] = []
    unreadable = False
    for snapshot in snapshots:
        if _deadline_reached(deadline):
            return owners, codes, True
        reading = _read_lsp_owner(snapshot, now, deadline)
        unreadable = unreadable or reading.unreadable
        if reading.stop:
            return owners, codes, True
        owners.append(reading.record)
        codes.extend(reading.codes)
    return owners, codes, unreadable


def _lsp_result(owners: list[dict], codes: list[str], *, unreadable: bool) -> dict:
    if unreadable and "lsp_state_unreadable" not in codes:
        codes.append("lsp_state_unreadable")
    message = "LSP runtime owners are live or retained."
    status = "degraded"
    if not codes:
        message, status = "LSP runtime owners are bounded.", "ok"
    return _result(
        "lsp",
        status,
        message,
        {
            "codes": codes,
            "owners": owners,
            "deletion_codes": list(codes),
            "read_error": unreadable,
        },
    )


def _lsp_runtime_check(
    state_root: Path,
    now: datetime,
    *,
    deadline: float = float("inf"),
) -> dict:
    """Bound the live and retained LSP owner evidence under run/lsp."""
    snapshots, unreadable, absent = _snapshot_lsp_runtime(state_root, deadline)
    if _deadline_reached(deadline):
        unreadable = True
        absent = False
    if absent:
        return _result(
            "lsp",
            "ok",
            "No LSP runtime owners are present.",
            {"codes": [], "owners": [], "deletion_codes": [], "read_error": False},
        )
    owners, codes, scan_unreadable = _scan_lsp_owners(snapshots, now, deadline)
    return _lsp_result(owners, codes, unreadable=unreadable or scan_unreadable)


def _index_deferred(message: str, detail: str) -> dict:
    return _result(
        "index",
        "degraded",
        message,
        {
            "exists": True,
            "freshness": "unknown",
            "age_seconds": None,
            "repairable": False,
            detail: True,
        },
    )


def _generation_result(
    status: str,
    message: str,
    **details: object,
) -> dict:
    baseline = {
        "catalog": "unknown",
        "active_generation": None,
        "catalog_schema": "unknown",
        "generation_schema": None,
        "source_manifest": "unknown",
        "evidence_integrity": "unknown",
        "search_index": "unknown",
        "search_schema": None,
        "search_integrity": "unknown",
        "vector_state": "unknown",
        "vector_model": None,
        "vector_dimensions": None,
        "freshness": "unknown",
        "unindexed_delta": None,
        "unresolved_observations": None,
        "age_seconds": None,
        "age_source": None,
        "repairable": status == "degraded",
    }
    baseline.update(details)
    return _result("generation", status, message, baseline)


def _maintenance_extractor_identity() -> str:
    import code_extractor
    import code_languages
    import corpus_snapshot

    inputs = {
        "classifier": code_languages.CLASSIFIER_IDENTITY,
        "code_extractor": code_extractor.EXTRACTOR_VERSION,
        "corpus_extractor": corpus_snapshot.EXTRACTOR_VERSION,
    }
    digest = hashlib.sha256(reliable_memory.canonical_json_bytes(inputs)).hexdigest()
    return f"maintenance-extractors/v3:{digest}"


class _GenerationFacts(NamedTuple):
    delta: int
    unresolved: int
    scope_state: str
    corpus_extraction_state: str
    graph_extraction_state: str


def _require_catalog_integrity(database: sqlite3.Connection, deadline: float) -> None:
    integrity = database.execute("PRAGMA integrity_check(1)").fetchone()
    tables = _tables(database, deadline)
    required = {"generations", "catalog_state", "activation_history"}
    if integrity is None or integrity[0] != "ok" or not required.issubset(tables):
        raise sqlite3.DatabaseError("catalog integrity or schema failed")


def _require_catalog_durability(database: sqlite3.Connection) -> None:
    journal = str(database.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
    synchronous = database.execute("PRAGMA synchronous").fetchone()[0]
    if journal != "delete" or synchronous != 2:
        raise sqlite3.DatabaseError("catalog durability contract failed")


def _active_pointer(database: sqlite3.Connection) -> object:
    row = database.execute(
        "SELECT active_generation_id FROM catalog_state WHERE singleton=1"
    ).fetchone()
    if row is None:
        return None
    return row[0]


def _catalog_active_generation(
    catalog_path: Path, state_root: Path, deadline: float, state: dict
) -> tuple[dict | None, str | None]:
    """The active generation id, or the result to return when there is none."""
    with _readonly_database(catalog_path, state_root, deadline=deadline) as database:
        database.set_progress_handler(lambda: int(_deadline_reached(deadline)), 1000)
        _require_catalog_integrity(database, deadline)
        _require_catalog_durability(database)
        active = _active_pointer(database)
        if not isinstance(active, str) or not active:
            return (
                _generation_result(
                    "ok",
                    "Evidence generation has not been activated; legacy retrieval remains available.",
                    catalog="valid",
                    catalog_schema="valid",
                    freshness="missing",
                    repairable=True,
                    recommended_action="rebuild_generation",
                ),
                None,
            )
        registered = database.execute(
            "SELECT 1 FROM generations WHERE generation_id=?",
            (active,),
        ).fetchone()
        if registered is None:
            raise sqlite3.DatabaseError("active generation is not registered")
        state["invalid_details"].update(
            catalog="valid",
            active_generation=active,
            catalog_schema="valid",
        )
    return None, active


def _validated_generation_manifest(
    generation_path: Path, state_root: Path, deadline: float, state: dict
) -> tuple[dict, tuple]:
    import generation_catalog

    diagnostic_value = json.loads(
        read_runtime_bytes(
            generation_path / "manifest.json",
            state_root,
            max_bytes=MAX_MANIFEST_BYTES,
        )
    )
    if isinstance(diagnostic_value, dict):
        state["diagnostic_manifest"] = diagnostic_value
        state["invalid_details"]["generation_schema"] = diagnostic_value.get(
            "graph_schema_version"
        )
    return generation_catalog._validate_generation(  # noqa: SLF001
        generation_path,
        state_root,
        deadline=deadline,
    )


def _scope_state(manifest: dict, repository_scope: object) -> str:
    if manifest.get("repository_scope") == repository_scope.as_dict():
        return "current"
    if "repository_scope" not in manifest:
        return "missing"
    return "mismatched"


def _corpus_extraction_state(
    manifest: dict, collector_version: str, extractor_version: str
) -> str:
    if manifest.get("collector_version") != collector_version:
        return "stale"
    if manifest.get("extractor_version") != extractor_version:
        return "stale"
    return "current"


def _graph_extraction_state(manifest: dict) -> str:
    if manifest.get("graph_extractor_version") == _maintenance_extractor_identity():
        return "current"
    return "stale"


def _source_delta(source_manifest: dict, snapshot: object) -> int:
    indexed = {
        item["relative_path"]: item["sha256"] for item in source_manifest["sources"]
    }
    current = {
        source.record.relative_path: source.record.sha256
        for source in snapshot.sources
    }
    return sum(
        indexed.get(path) != current.get(path)
        for path in indexed.keys() | current.keys()
    )


def _unresolved_observations(
    generation_path: Path, state_root: Path, deadline: float
) -> int:
    graph = generation_path / "evidence.sqlite3"
    with _readonly_database(
        graph, state_root, max_bytes=16 * 1024 * 1024 * 1024, deadline=deadline
    ) as database:
        database.set_progress_handler(lambda: int(_deadline_reached(deadline)), 1000)
        return database.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM observation LIMIT ?)",
            (MAX_OPERATIONAL_ROWS + 1,),
        ).fetchone()[0]


def _generation_facts(
    root: Path,
    state_root: Path,
    generation_path: Path,
    manifest: dict,
    max_sources: int,
    deadline: float,
    cancelled,
) -> _GenerationFacts:
    """What the live sources and the stored generation currently say."""
    source_manifest = json.loads(
        read_runtime_bytes(
            generation_path / "source-manifest.json",
            state_root,
            max_bytes=MAX_MANIFEST_BYTES * 1024,
        )
    )
    policy = source_manifest["policy"]

    from corpus_snapshot import COLLECTOR_VERSION, EXTRACTOR_VERSION, collect_corpus
    from repository_scope import resolve_repository_scope

    repository_scope = resolve_repository_scope(
        root, deadline=deadline, cancelled=cancelled
    )
    snapshot = collect_corpus(
        root,
        daily_paths=policy["daily_paths"],
        code_roots=policy["code_roots"],
        include_historical=policy["include_historical"],
        as_of=policy["as_of"],
        max_files=max_sources,
        deadline=deadline,
    )
    return _GenerationFacts(
        delta=_source_delta(source_manifest, snapshot),
        unresolved=_unresolved_observations(generation_path, state_root, deadline),
        scope_state=_scope_state(manifest, repository_scope),
        corpus_extraction_state=_corpus_extraction_state(
            manifest, COLLECTOR_VERSION, EXTRACTOR_VERSION
        ),
        graph_extraction_state=_graph_extraction_state(manifest),
    )


def _generation_age_source(seal: tuple, catalog_info) -> tuple[int, str]:
    manifest_seal = next(
        (entry for entry in seal if entry.path == "manifest.json"), None
    )
    if manifest_seal is not None:
        return manifest_seal.mtime_ns, "manifest_mtime"
    if catalog_info is not None:
        return catalog_info.st_mtime_ns, "catalog_mtime"
    raise OSError("generation age timestamp is unavailable")


def _generation_age(seal: tuple, catalog_info, now: datetime) -> tuple[int, str]:
    timestamp_ns, age_source = _generation_age_source(seal, catalog_info)
    now_ns = int(_as_utc(now).timestamp() * 1_000_000_000)
    return max(0, (now_ns - timestamp_ns) // 1_000_000_000), age_source


def _identity_stale(facts: _GenerationFacts, complete_v2: bool) -> bool:
    if facts.scope_state != "current" or facts.corpus_extraction_state != "current":
        return True
    return facts.graph_extraction_state != "current" or not complete_v2


def _generation_search_fields(complete_v2: bool) -> dict:
    if not complete_v2:
        return {
            "search_index": "missing",
            "search_schema": None,
            "search_integrity": "missing",
        }
    return {
        "search_index": "valid",
        "search_schema": "corpus-search/v1",
        "search_integrity": "valid",
    }


def _generation_health_result(
    active: str,
    manifest: dict,
    seal: tuple,
    catalog_info,
    now: datetime,
    facts: _GenerationFacts,
) -> dict:
    age, age_source = _generation_age(seal, catalog_info, now)
    vector_state = str(manifest["vector_state"])
    complete_v2 = manifest.get("schema_version") == "corpus-generation/v2"
    stale = bool(
        facts.delta
        or age > GENERATION_FRESH_SECONDS
        or _identity_stale(facts, complete_v2)
    )
    degraded = stale or vector_state == "stale" or bool(facts.unresolved)
    message = "Evidence generation is healthy."
    if degraded:
        message = "Evidence generation requires refresh."
    return _generation_result(
        "degraded" if degraded else "ok",
        message,
        catalog="valid",
        active_generation=active,
        catalog_schema="valid",
        generation_schema=manifest["graph_schema_version"],
        source_manifest="valid",
        evidence_integrity="valid",
        vector_state=vector_state,
        vector_model=manifest["embedding_model_id"],
        vector_dimensions=manifest["vector_dimensions"],
        freshness="stale" if stale else "fresh",
        repository_scope=facts.scope_state,
        extraction_identity=facts.graph_extraction_state,
        corpus_extraction_identity=facts.corpus_extraction_state,
        unindexed_delta=facts.delta,
        unresolved_observations=facts.unresolved,
        age_seconds=age,
        age_source=age_source,
        repairable=degraded,
        **_generation_search_fields(complete_v2),
    )


def _validated_search_artifact(
    generation_path: Path, diagnostic: dict, state_root: Path, deadline: float
) -> dict:
    try:
        from search_memory import validate_generation_fts_artifact

        validate_generation_fts_artifact(
            generation_path,
            diagnostic,
            state_root=state_root,
            deadline=deadline,
        )
    except (OSError, PermissionError, TypeError, ValueError, sqlite3.Error):
        return {"search_index": "corrupt", "search_integrity": "invalid"}
    return {"search_index": "valid", "search_integrity": "valid"}


def _diagnostic_search_state(
    generation_path: Path, diagnostic: dict, state_root: Path, deadline: float
) -> dict:
    search_kind = _safe_kind(generation_path / "search.sqlite3", state_root)[0]
    if search_kind == "missing":
        return {"search_index": "missing", "search_integrity": "missing"}
    if search_kind != "regular":
        return {"search_index": "corrupt", "search_integrity": "invalid"}
    return _validated_search_artifact(
        generation_path, diagnostic, state_root, deadline
    )


def _diagnose_invalid_generation(
    state: dict, state_root: Path, deadline: float
) -> None:
    """Say what the search artifact looks like when the generation is invalid."""
    generation_path = state["generation_path"]
    diagnostic = state["diagnostic_manifest"]
    if generation_path is None or not isinstance(diagnostic, dict):
        return
    if diagnostic.get("schema_version") != "corpus-generation/v2":
        return
    state["invalid_details"]["search_schema"] = "corpus-search/v1"
    state["invalid_details"].update(
        _diagnostic_search_state(generation_path, diagnostic, state_root, deadline)
    )


def _checked_generation(
    root: Path,
    state_root: Path,
    now: datetime,
    deadline: float,
    catalog_path: Path,
    catalog_info,
    max_sources: int,
    cancelled,
    state: dict,
) -> dict:
    early, active = _catalog_active_generation(
        catalog_path, state_root, deadline, state
    )
    if early is not None:
        return early
    if _deadline_reached(deadline):
        raise TimeoutError("generation check deadline")
    generation_path = state_root / "cache" / "evidence-graph" / "generations" / active
    state["generation_path"] = generation_path
    manifest, seal = _validated_generation_manifest(
        generation_path, state_root, deadline, state
    )
    facts = _generation_facts(
        root, state_root, generation_path, manifest, max_sources, deadline, cancelled
    )
    return _generation_health_result(active, manifest, seal, catalog_info, now, facts)


def _require_positive_source_limit(max_sources: object) -> None:
    if (
        isinstance(max_sources, bool)
        or not isinstance(max_sources, int)
        or max_sources < 1
    ):
        raise ValueError("max_sources must be a positive integer")


def _generation_check(
    root: Path,
    state_root: Path,
    now: datetime,
    deadline: float = float("inf"),
    *,
    max_sources: int = DEFAULT_GENERATION_SOURCE_LIMIT,
    cancelled=None,
) -> dict:
    """Validate the catalog-selected immutable generation without writing."""
    _require_positive_source_limit(max_sources)
    catalog_path = state_root / "cache" / "evidence-graph" / "catalog.sqlite3"
    kind, catalog_info = _safe_kind(catalog_path, state_root)
    if kind == "missing":
        return _generation_result(
            "ok",
            "Evidence generation has not been built; legacy retrieval remains available.",
            catalog="missing",
            freshness="missing",
            repairable=True,
            recommended_action="rebuild_generation",
        )
    if kind != "regular":
        return _generation_result(
            "error",
            "Evidence generation catalog is unsafe.",
            catalog="invalid",
            repairable=False,
        )
    state: dict[str, Any] = {
        "invalid_details": {"catalog": "invalid", "repairable": True},
        "diagnostic_manifest": None,
        "generation_path": None,
    }
    try:
        return _checked_generation(
            root,
            state_root,
            now,
            deadline,
            catalog_path,
            catalog_info,
            max_sources,
            cancelled,
            state,
        )
    except TimeoutError:
        return _generation_result(
            "degraded",
            "Evidence generation check was deferred by its time bound.",
            catalog="valid",
            budget_exhausted=True,
            partial=True,
            repairable=False,
        )
    except (
        KeyError,
        OSError,
        PermissionError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        sqlite3.Error,
    ):
        _diagnose_invalid_generation(state, state_root, deadline)
        return _generation_result(
            "error",
            "Evidence generation catalog or active artifacts are invalid.",
            **state["invalid_details"],
        )


class _ManifestState(NamedTuple):
    state: str
    matches: bool
    deferred: dict | None


class _IndexInspection(NamedTuple):
    early: dict | None
    manifest_state: str
    manifest_matches: bool


def _corrupt_index_result() -> dict:
    return _result(
        "index",
        "error",
        "FTS index is corrupt or unsafe.",
        {
            "exists": True,
            "freshness": "corrupt",
            "age_seconds": None,
            "repairable": False,
        },
    )


def _index_path_limit_result() -> dict:
    return _result(
        "index",
        "degraded",
        "FTS index path validation reached its safety limit.",
        {
            "exists": True,
            "freshness": "unknown",
            "age_seconds": None,
            "repairable": False,
            "path_limit_exceeded": True,
        },
    )


def _unusable_index(kind: str, info) -> bool:
    return kind != "regular" or info is None or info.st_size == 0


def _index_artifact_state(kind: str, info, deadline: float) -> dict | None:
    """The result to return when the index cannot be inspected at all."""
    if kind == "missing":
        return _result(
            "index",
            "degraded",
            "FTS index is missing.",
            {
                "exists": False,
                "freshness": "missing",
                "age_seconds": None,
                "repairable": True,
            },
        )
    if _unusable_index(kind, info):
        return _corrupt_index_result()
    if time.monotonic() >= deadline:
        return _index_deferred(
            "FTS index check exceeded its time budget.", "budget_exhausted"
        )
    return None


def _require_index_schema(connection: sqlite3.Connection) -> None:
    quick_check = connection.execute("PRAGMA quick_check(1)").fetchone()
    if not quick_check or quick_check[0] != "ok":
        raise sqlite3.DatabaseError("quick_check failed")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(pages)")}
    if not INDEX_COLUMNS.issubset(columns):
        raise sqlite3.DatabaseError("unexpected index schema")


def _index_probe(index: Path, state_root: Path, deadline: float) -> list:
    """The indexed paths, after proving the artifact answers a real query."""
    connection = _readonly_database(
        index,
        state_root,
        max_bytes=MAX_INDEX_DB_BYTES,
        deadline=deadline,
    )
    try:
        connection.set_progress_handler(
            lambda: int(time.monotonic() >= deadline), 1000
        )
        _require_index_schema(connection)
        connection.execute(
            "SELECT rowid FROM pages WHERE pages MATCH ? LIMIT 1",
            ("__doctor_integrity_probe__",),
        ).fetchone()
        cursor = connection.execute("SELECT path FROM pages")
        return cursor.fetchmany(MAX_INDEX_PATHS + 1)
    finally:
        connection.close()


def _valid_manifest_paths(manifest_error: object, manifest_paths: object) -> bool:
    if manifest_error:
        return False
    return all(isinstance(item, str) for item in (manifest_paths or []))


def _index_manifest_state(
    manifest: Path, state_root: Path, indexed_paths: list[str], deadline: float
) -> _ManifestState:
    manifest_kind, _ = _safe_kind(manifest, state_root)
    if manifest_kind == "missing":
        return _ManifestState("missing", False, None)
    manifest_paths, manifest_error = _read_bounded_json(
        manifest,
        state_root,
        max_bytes=MAX_MANIFEST_BYTES,
        expected_type=list,
        deadline=deadline,
    )
    if manifest_error == "budget":
        deferred = _index_deferred(
            "FTS manifest check exceeded its time budget.", "budget_exhausted"
        )
        return _ManifestState("missing", False, deferred)
    if not _valid_manifest_paths(manifest_error, manifest_paths):
        return _ManifestState("invalid", False, None)
    if sorted(manifest_paths) != sorted(indexed_paths):
        return _ManifestState("mismatch", False, None)
    return _ManifestState("current", True, None)


def _inspect_index(
    index: Path, state_root: Path, manifest: Path, deadline: float
) -> _IndexInspection:
    indexed_rows = _index_probe(index, state_root, deadline)
    if len(indexed_rows) > MAX_INDEX_PATHS:
        return _IndexInspection(_index_path_limit_result(), "missing", False)
    indexed_paths = [row[0] for row in indexed_rows]
    if any(not isinstance(item, str) for item in indexed_paths):
        raise ValueError("invalid indexed path")
    manifest_state = _index_manifest_state(
        manifest, state_root, indexed_paths, deadline
    )
    return _IndexInspection(
        manifest_state.deferred, manifest_state.state, manifest_state.matches
    )


def _operational_index_result(exc: sqlite3.OperationalError, deadline: float) -> dict:
    lowered = str(exc).lower()
    # A lock is a lock even when waiting for it used up the budget, and it
    # is the more actionable of the two verdicts.
    if "locked" in lowered or "busy" in lowered:
        return _index_deferred("FTS index is busy.", "database_busy")
    if time.monotonic() >= deadline or "interrupted" in lowered:
        return _index_deferred(
            "FTS index check exceeded its time budget.", "budget_exhausted"
        )
    return _corrupt_index_result()


def _source_rebuild_required(
    index: Path, state_root: Path, manifest: Path, root: Path | None, deadline: float
) -> bool | None:
    """None when the source freshness cannot be determined at all."""
    source_root = Path(root or state_root)
    try:
        import search_memory

        pages = search_memory._collect_pages(  # noqa: SLF001
            "all",
            knowledge_dir=source_root / "knowledge" / "notes",
            root=source_root,
            deadline=deadline,
        )
        return search_memory._needs_rebuild(  # noqa: SLF001
            pages,
            root=source_root,
            index_file=index,
            index_manifest=manifest,
            deadline=deadline,
        )
    except (OSError, ValueError, sqlite3.Error):
        return None


def _index_timestamp(info) -> datetime | None:
    try:
        return datetime.fromtimestamp(info.st_mtime, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _stale_source_index_result(age: int, manifest_state: str) -> dict:
    return _result(
        "index",
        "degraded",
        "FTS index is stale relative to searchable knowledge sources.",
        {
            "exists": True,
            "freshness": "stale",
            "age_seconds": age,
            "repairable": True,
            "source_rebuild_required": True,
            "source_contract": "path-manifest+mtime",
            "manifest": manifest_state,
        },
    )


def _aged_index_result(age: int, manifest_state: str) -> dict:
    fresh = age <= INDEX_FRESH_SECONDS
    freshness = "fresh" if fresh else "stale"
    return _result(
        "index",
        "ok" if fresh else "degraded",
        "FTS index is fresh." if fresh else "FTS index is stale.",
        {
            "exists": True,
            "freshness": freshness,
            "age_seconds": age,
            "repairable": not fresh,
            "source_rebuild_required": False,
            "source_contract": "path-manifest+mtime",
            "manifest": manifest_state,
        },
    )


def _index_freshness_result(
    index: Path,
    state_root: Path,
    manifest: Path,
    info,
    now: datetime,
    deadline: float,
    inspection: _IndexInspection,
    root: Path | None,
) -> dict:
    rebuild_required = _source_rebuild_required(
        index, state_root, manifest, root, deadline
    )
    if rebuild_required is None:
        return _index_deferred(
            "FTS source freshness could not be determined.",
            "source_freshness_unknown",
        )
    timestamp = _index_timestamp(info)
    if timestamp is None:
        return _corrupt_index_result()
    age = max(0, int((now - timestamp).total_seconds()))
    if rebuild_required or not inspection.manifest_matches:
        return _stale_source_index_result(age, inspection.manifest_state)
    return _aged_index_result(age, inspection.manifest_state)


def _index_check(
    state_root: Path,
    now: datetime,
    deadline: float = float("inf"),
    *,
    root: Path | None = None,
) -> dict:
    index = state_root / "cache" / "index.sqlite"
    kind, info = _safe_kind(index, state_root)
    unusable = _index_artifact_state(kind, info, deadline)
    if unusable is not None:
        return unusable
    manifest = state_root / "cache" / ".paths-manifest"
    try:
        inspection = _inspect_index(index, state_root, manifest, deadline)
    except sqlite3.OperationalError as exc:
        return _operational_index_result(exc, deadline)
    except (
        OSError,
        OverflowError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        sqlite3.Error,
    ):
        return _corrupt_index_result()
    if inspection.early is not None:
        return inspection.early
    return _index_freshness_result(
        index, state_root, manifest, info, now, deadline, inspection, root
    )


def _read_state(state_root: Path, deadline: float) -> tuple[dict, str | None]:
    path = state_root / "run" / "state.json"
    if _safe_kind(path, state_root)[0] == "missing":
        return {}, None
    value, problem = _read_bounded_json(
        path,
        state_root,
        max_bytes=MAX_STATE_BYTES,
        deadline=deadline,
    )
    return (value or {}, problem)


def _capture_check(state_root: Path, deadline: float) -> dict:
    """Report captures the hooks lost, so a silent loss is visible in health."""
    from capture_diagnostics import capture_failure_totals

    state, state_error = _read_state(state_root, deadline)
    totals = capture_failure_totals(state)
    lost = sum(totals.values())
    details: dict[str, Any] = {
        "lost": lost,
        "kinds": totals,
        "trail": "logs/capture-failures.jsonl",
        "state_error": state_error,
    }
    if state_error:
        return _result(
            "capture",
            "degraded",
            "Capture diagnostics could not be read within safety bounds.",
            details,
        )
    if lost:
        return _result("capture", "degraded", f"{lost} capture(s) were lost.", details)
    return _result("capture", "ok", "No lost capture is recorded.", details)


def _scheduler_check(root: Path, state_root: Path, now: datetime, deadline: float) -> dict:
    scripts = {
        "scheduled_nightly": (root / "scripts" / "scheduled_nightly.py").is_file(),
        "search_memory": (root / "scripts" / "search_memory.py").is_file(),
    }
    state, state_error = _read_state(state_root, deadline)
    details: dict[str, Any] = {
        "scripts": scripts,
        "last_nightly_date": state.get("last_nightly_date"),
        "last_nightly_status": state.get("last_nightly_status", "unknown"),
        "last_nightly_skip": state.get("last_nightly_skip"),
        "state_error": state_error,
    }
    if state_error in {"budget", "oversized"}:
        return _result(
            "scheduler",
            "degraded",
            "Maintenance state could not be fully checked within safety bounds.",
            details,
        )
    if not all(scripts.values()) or state_error:
        return _result(
            "scheduler", "error", "Maintenance source or local state is invalid.", details
        )
    status = state.get("last_nightly_status")
    last_date = str(state.get("last_nightly_date", ""))[:10]
    if status == "failed":
        return _result("scheduler", "error", "Last nightly maintenance failed.", details)
    if not status or not last_date:
        return _result("scheduler", "skipped", "Nightly maintenance status is unknown.", details)
    today = now.date().isoformat()
    if status in {"ok", "success"} and last_date == today:
        return _result("scheduler", "ok", "Nightly maintenance is current.", details)
    return _result("scheduler", "degraded", "Nightly maintenance is stale.", details)


def _mcp_check(root: Path) -> dict:
    source = (root / "scripts" / "mcp_server.py").is_file()
    try:
        package = importlib.util.find_spec("mcp") is not None
    except (ImportError, ValueError):
        package = False
    details = {
        "source": "ok" if source else "error",
        "package": "ok" if package else "skipped",
        "capability": "available" if package else "optional dependency not installed",
        "core_capture_required": False,
    }
    status = "ok" if source else "error"
    message = (
        "MCP source is available; package capability was detected."
        if source
        else "MCP server source is missing."
    )
    return _result("mcp", status, message, details)


def _contains_markers(path: Path, markers: tuple[str, ...]) -> bool:
    kind, info = _safe_kind(path, path.parent)
    if kind != "regular" or info is None or info.st_size > MAX_CONFIG_BYTES:
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    lowered = text.lower()
    return all(marker.lower() in lowered for marker in markers)


def _parse_toml_document(text: str) -> tuple[dict[str, Any] | None, str | None]:
    parser = STDLIB_TOML or TOMLI
    if parser is None:
        return None, "toml_parser_unavailable"
    try:
        document = parser.loads(text)
    except (ValueError, TypeError):
        return None, "toml_invalid"
    return (document, None) if isinstance(document, dict) else (None, "toml_invalid")


_CODEX_SERVER_ARG_PREFIX = ["run", "--locked", "--no-sync", "--directory"]
_CODEX_SERVER_ARG_SUFFIX = ["python", "scripts/mcp_server.py"]


def _codex_server_arg_shape(args: object) -> bool:
    if not isinstance(args, list) or len(args) != 8:
        return False
    return all(isinstance(item, str) for item in args)


def _codex_server_args_match(args: object) -> bool:
    if not _codex_server_arg_shape(args):
        return False
    if args[:4] != _CODEX_SERVER_ARG_PREFIX:
        return False
    return args[5:] == _CODEX_SERVER_ARG_SUFFIX


def _codex_server_configured(table: dict) -> bool:
    if table.get("command") != "uv":
        return False
    if table.get("enabled", True) is not True:
        return False
    return _codex_server_args_match(table.get("args"))


def _codex_config_state(path: Path) -> tuple[bool | None, str]:
    kind, info = _safe_kind(path, path.parent)
    if kind != "regular" or info is None or info.st_size > MAX_CONFIG_BYTES:
        return False, "config_missing_or_unsafe"
    try:
        raw = read_stable_bytes(path, MAX_CONFIG_BYTES, label="Codex config")
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return False, "config_missing_or_unsafe"
    document, error = _parse_toml_document(text)
    if error is not None:
        return (None, error) if error == "toml_parser_unavailable" else (False, error)
    assert document is not None
    servers = document.get("mcp_servers")
    table = servers.get("llm-wiki") if isinstance(servers, dict) else None
    if not isinstance(table, dict):
        return False, "target_missing_or_invalid"
    configured = _codex_server_configured(table)
    if configured:
        return True, "configured"
    return False, "target_missing_or_invalid"


def _codex_app_server_command(*, platform: str = os.name) -> list[str] | None:
    arguments = ["app-server", "--listen", "stdio://"]
    if platform == "nt":
        executable = shutil.which("codex.exe")
        if executable:
            return [executable, *arguments]
        shim = shutil.which("codex.cmd")
        command_processor = os.environ.get("ComSpec")
        if shim and command_processor:
            command_line = subprocess.list2cmdline([shim, *arguments])
            return [command_processor, "/d", "/s", "/c", command_line]
        return None
    executable = shutil.which("codex")
    return [executable, *arguments] if executable else None


_PROBE_INCOMPLETE = object()


class _CodexProbeStreams(NamedTuple):
    readers: list
    captured: dict
    overflow: threading.Event


def _codex_probe_payload(root: Path) -> bytes:
    requests = (
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "llm-wiki-doctor",
                    "title": "LLM-Wiki Doctor",
                    "version": SCHEMA_VERSION,
                },
                "capabilities": {"experimentalApi": True},
            },
        },
        {"method": "initialized", "params": {}},
        {"id": 2, "method": "hooks/list", "params": {"cwds": [str(root)]}},
    )
    return "".join(
        json.dumps(item, separators=(",", ":")) + "\n" for item in requests
    ).encode("utf-8")


def _kill_and_reap(process: Any, probe_deadline: float) -> None:
    process.kill()
    try:
        process.wait(timeout=max(0.0, probe_deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        pass


def _codex_pipes_missing(process: Any) -> bool:
    return (
        process.stdin is None or process.stdout is None or process.stderr is None
    )


def _start_codex_readers(process: Any) -> _CodexProbeStreams:
    """Drain both pipes into bounded buffers so the child cannot block on us."""
    overflow = threading.Event()
    captured = {"stdout": bytearray(), "stderr": bytearray()}

    def drain(name: str, stream: Any) -> None:
        try:
            while chunk := stream.read(8192):
                remaining = MAX_CODEX_HOOK_PROBE_BYTES - len(captured[name])
                if len(chunk) > remaining:
                    captured[name].extend(chunk[: max(0, remaining)])
                    overflow.set()
                    process.kill()
                    return
                captured[name].extend(chunk)
        except OSError:
            overflow.set()
            process.kill()

    readers = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    return _CodexProbeStreams(readers, captured, overflow)


def _await_codex_process(
    process: Any, payload: bytes, probe_deadline: float
) -> bool:
    """True when the probe process finished within its own deadline."""
    if _deadline_reached(probe_deadline):
        _kill_and_reap(process, probe_deadline)
        return False
    process.stdin.write(payload)
    process.stdin.flush()
    process.stdin.close()
    try:
        process.wait(timeout=max(0.0, probe_deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        _kill_and_reap(process, probe_deadline)
        return False
    return True


def _readers_still_running(readers: list) -> bool:
    return any(reader.is_alive() for reader in readers)


def _clean_codex_exit(process: Any, streams: _CodexProbeStreams) -> bool:
    return process.returncode == 0 and not streams.overflow.is_set()


def _drained_codex_output(
    process: Any, streams: _CodexProbeStreams, probe_deadline: float
) -> bytes | object | None:
    for reader in streams.readers:
        reader.join(timeout=max(0.0, probe_deadline - time.monotonic()))
    if _readers_still_running(streams.readers):
        if process.returncode is None:
            process.kill()
        return _PROBE_INCOMPLETE
    if not _clean_codex_exit(process, streams):
        return None
    return bytes(streams.captured["stdout"])


def _run_codex_probe(
    command: list[str], root: Path, home: Path, probe_deadline: float
) -> bytes | object | None:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(home / ".codex")
    payload = _codex_probe_payload(root)
    try:
        process = subprocess.Popen(  # noqa: S603
            command,
            cwd=str(root),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if _codex_pipes_missing(process):
            _kill_and_reap(process, probe_deadline)
            return _PROBE_INCOMPLETE
        streams = _start_codex_readers(process)
        if not _await_codex_process(process, payload, probe_deadline):
            return _PROBE_INCOMPLETE
        return _drained_codex_output(process, streams, probe_deadline)
    except (OSError, PermissionError, subprocess.SubprocessError, ValueError):
        return _PROBE_INCOMPLETE


def _codex_hooks_message(line: str) -> tuple[bool, dict | None]:
    message = json.loads(line)
    if not isinstance(message, dict) or message.get("id") != 2:
        return False, None
    result = message.get("result")
    if isinstance(result, dict):
        return True, result
    return True, None


def _codex_hooks_result(raw: bytes) -> dict | None:
    try:
        for line in raw.decode("utf-8").splitlines():
            found, result = _codex_hooks_message(line)
            if found:
                return result
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return None


def _codex_deadline_result(deadline: float) -> object | None:
    if _deadline_reached(deadline):
        return _CODEX_PROBE_NOT_COMPLETED
    return None


def _probe_codex_hooks_list(
    root: Path, home: Path, *, deadline: float = float("inf")
) -> dict[str, Any] | object | None:
    started_at = time.monotonic()
    probe_deadline = min(deadline, started_at + CODEX_HOOK_PROBE_SECONDS)
    if probe_deadline - started_at < CODEX_HOOK_PROBE_STARTUP_SECONDS:
        return None
    command = _codex_app_server_command()
    if command is None:
        return None
    raw = _run_codex_probe(command, root, home, probe_deadline)
    if raw is _PROBE_INCOMPLETE:
        return _codex_deadline_result(deadline)
    if raw is None:
        return None
    return _codex_hooks_result(raw)


def _single_template_group(groups: object) -> dict:
    if not isinstance(groups, list) or len(groups) != 1:
        raise ValueError("invalid Codex hook template")
    group = groups[0]
    if not isinstance(group, dict):
        raise ValueError("invalid Codex hook template")
    return group


def _single_template_handler(groups: object) -> tuple[dict, dict]:
    """The one group and its one handler, or a template error."""
    group = _single_template_group(groups)
    handlers = group.get("hooks")
    if not isinstance(handlers, list) or len(handlers) != 1:
        raise ValueError("invalid Codex hook template")
    handler = handlers[0]
    if not isinstance(handler, dict):
        raise ValueError("invalid Codex hook template")
    return group, handler


def _template_hook_command(handler: dict) -> str:
    command_key = "commandWindows" if os.name == "nt" else "command"
    command = handler.get(command_key)
    if not isinstance(command, str):
        raise ValueError("invalid Codex hook template")
    return command


def _template_hooks_table(template_path: Path) -> dict:
    value, problem = _read_bounded_json(
        template_path,
        template_path.parent,
        max_bytes=MAX_CONFIG_BYTES,
    )
    if problem or not isinstance(value, dict):
        raise ValueError("invalid Codex hook template")
    hooks = value.get("hooks")
    if not isinstance(hooks, dict):
        raise ValueError("invalid Codex hook template")
    return hooks


def _expected_codex_runtime_hooks(template_path: Path) -> list[dict[str, Any]]:
    expected = []
    for event_name, groups in _template_hooks_table(template_path).items():
        if not isinstance(event_name, str):
            raise ValueError("invalid Codex hook template")
        group, handler = _single_template_handler(groups)
        expected.append(
            {
                "eventName": event_name,
                "matcher": group.get("matcher"),
                "command": _template_hook_command(handler),
            }
        )
    return expected


def _single_probe_entry(data: object) -> dict | None:
    if not isinstance(data, list) or len(data) != 1:
        return None
    if not isinstance(data[0], dict):
        return None
    return data[0]


def _codex_probe_entry(response: object) -> tuple[dict | None, str]:
    """The one probe entry, or the code explaining why there is none."""
    if response is _CODEX_PROBE_NOT_COMPLETED:
        return None, "runtime_hooks_not_completed"
    if response is None:
        return None, "runtime_hooks_unverified"
    assert isinstance(response, dict)
    entry = _single_probe_entry(response.get("data"))
    if entry is None:
        return None, "runtime_hooks_invalid"
    return entry, ""


def _codex_entry_problem(entry: dict, root: Path) -> str:
    """The failure code for this entry, or an empty string when it is usable."""
    try:
        if Path(entry.get("cwd", "")).resolve() != root.resolve():
            return "runtime_hooks_wrong_cwd"
    except (OSError, TypeError, ValueError):
        return "runtime_hooks_invalid"
    if entry.get("warnings") or entry.get("errors"):
        return "runtime_hooks_warning_or_error"
    return ""


def _codex_hook_list(entry: dict) -> list | None:
    hooks = entry.get("hooks")
    if not isinstance(hooks, list) or any(
        not isinstance(item, dict) for item in hooks
    ):
        return None
    return hooks


def _codex_owned_hook(hook: dict) -> bool:
    command = hook.get("command")
    if not isinstance(command, str) or "codex_memory.py" not in command:
        return False
    return command.rstrip().endswith(" hook")


def _codex_owned_hooks(hooks: list) -> list:
    return [hook for hook in hooks if _codex_owned_hook(hook)]


def _expected_codex_hooks(root: Path, ours: list) -> tuple[list | None, str]:
    try:
        expected = _expected_codex_runtime_hooks(
            root / "integrations" / "codex" / "hooks.json"
        )
    except ValueError:
        return None, "runtime_hooks_template_invalid"
    if len(ours) != len(expected):
        return None, "runtime_hooks_mismatch"
    return expected, ""


def _matching_hooks(wanted: dict, ours: list) -> list:
    return [
        hook
        for hook in ours
        if all(hook.get(field) == value for field, value in wanted.items())
    ]


def _codex_hook_trust_code(trust: object) -> str:
    if trust in {"untrusted", "modified"}:
        return f"runtime_hooks_{trust}"
    return "runtime_hooks_trust_unknown"


def _codex_hook_problem(wanted: dict, ours: list) -> str:
    matches = _matching_hooks(wanted, ours)
    if len(matches) != 1:
        return "runtime_hooks_mismatch"
    hook = matches[0]
    if hook.get("enabled") is not True:
        return "runtime_hooks_disabled"
    trust = hook.get("trustStatus")
    if trust in {"trusted", "managed"}:
        return ""
    return _codex_hook_trust_code(trust)


def _codex_hooks_verdict(root: Path, ours: list) -> tuple[bool, str]:
    expected, problem = _expected_codex_hooks(root, ours)
    if expected is None:
        return False, problem
    for wanted in expected:
        problem = _codex_hook_problem(wanted, ours)
        if problem:
            return False, problem
    return True, "runtime_hooks_active"


def _codex_runtime_hooks_state(
    root: Path, home: Path, *, deadline: float = float("inf")
) -> tuple[bool, str]:
    if deadline - time.monotonic() < CODEX_HOOK_PROBE_STARTUP_SECONDS:
        return False, "runtime_hooks_not_completed"
    response = _probe_codex_hooks_list(root, home, deadline=deadline)
    entry, problem = _codex_probe_entry(response)
    if entry is None:
        return False, problem
    problem = _codex_entry_problem(entry, root)
    if problem:
        return False, problem
    hooks = _codex_hook_list(entry)
    if hooks is None:
        return False, "runtime_hooks_invalid"
    return _codex_hooks_verdict(root, _codex_owned_hooks(hooks))


def _codex_wrapper_configured(root: Path, home: Path) -> bool:
    if not (root / "scripts" / "codex-memory-wrapper.ps1").is_file():
        return False
    profiles = (
        home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1",
        home / "Documents" / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1",
        home / ".config" / "powershell" / "Microsoft.PowerShell_profile.ps1",
    )
    return any(
        _contains_markers(profile, ("codex-memory-wrapper.ps1", "LLM_WIKI_ROOT"))
        for profile in profiles
    )


def _integration_sources(root: Path) -> dict[str, Path]:
    return {
        "claude": root / "integrations" / "claude-code" / "settings.json",
        "opencode": root / "scripts" / "llm-wiki-memory-opencode.js",
        "codex": root / "integrations" / "codex" / "hooks.json",
        "cursor": root / "integrations" / "cursor" / "hooks.json",
        "antigravity": root / "integrations" / "antigravity" / "hooks.json",
    }


def _integration_host_configs(
    home: Path,
) -> dict[str, tuple[Path, list[tuple[Path, tuple[str, ...]]]]]:
    return {
        "claude": (
            home / ".claude",
            [
                (
                    home / ".claude" / "settings.json",
                    ("LLM_WIKI_ROOT", "integration_adapter.py"),
                )
            ],
        ),
        "opencode": (
            home / ".config" / "opencode",
            [
                (
                    home / ".config" / "opencode" / "plugins" / "llm-wiki-memory.js",
                    ("session.created", "LLM_WIKI_ROOT"),
                ),
            ],
        ),
        "codex": (
            home / ".codex",
            [
                (
                    home / ".codex" / "config.toml",
                    ("mcp_servers.llm-wiki", "mcp_server.py"),
                )
            ],
        ),
    }


def _codex_degraded_result(root: Path, home: Path, reason: str) -> dict[str, object]:
    wrapper = _codex_wrapper_configured(root, home)
    message = "Official Codex hooks are not verified and no capture fallback is configured."
    capture_mode = "none"
    if wrapper:
        message = "Official Codex hooks are not verified; wrapper fallback is heartbeat-only."
        capture_mode = "wrapper-fallback-heartbeat-only"
    result: dict[str, object] = {
        "status": "degraded",
        "message": message,
        "reason": reason,
        "capture_mode": capture_mode,
    }
    if reason == "runtime_hooks_not_completed":
        result["not_completed"] = True
    return result


def _codex_host_result(root: Path, home: Path, deadline: float) -> dict[str, object]:
    if not (home / ".codex").exists():
        return {"status": "skipped", "message": "Optional host not installed."}
    hooks_active, reason = _codex_runtime_hooks_state(root, home, deadline=deadline)
    if hooks_active:
        return {
            "status": "ok",
            "message": "Official Codex hooks are active and trusted; review changes in /hooks.",
            "capture_mode": "official-hooks",
            "trust": "review-with-/hooks",
        }
    return _codex_degraded_result(root, home, reason)


def _generic_host_result(
    host_dir: Path, configs: list[tuple[Path, tuple[str, ...]]]
) -> dict[str, object]:
    if not host_dir.exists():
        return {"status": "skipped", "message": "Optional host not installed."}
    if any(_contains_markers(path, markers) for path, markers in configs):
        return {"status": "ok", "message": "User integration config detected."}
    return {
        "status": "degraded",
        "message": "Host detected without LLM-Wiki config.",
    }


def _managed_ide_detected(home: Path, name: str) -> bool:
    paths = {
        "cursor": home / ".cursor",
        "antigravity": home / ".gemini" / "antigravity-ide",
    }
    return paths[name].is_dir()


def _managed_ide_resource(root: Path, home: Path, name: str):
    from integration_hook_config import managed_ide_hook_resources

    identifiers = {
        "cursor": "cursor-user-hooks",
        "antigravity": "antigravity-user-hooks",
    }
    resources = {
        resource.resource_id: resource for resource in managed_ide_hook_resources(root, home)
    }
    return resources[identifiers[name]]


def _managed_ide_conflict_result(detected: bool) -> dict[str, object]:
    return {
        "status": "degraded",
        "message": "Managed hook configuration is malformed, unsafe, or conflicting.",
        "configuration_status": "conflict",
        "host_detected": detected,
    }


def _managed_ide_active_result(detected: bool) -> dict[str, object]:
    statuses = {True: "ok", False: "skipped"}
    return {
        "status": statuses[detected],
        "message": "Managed local user hooks are active.",
        "capture_mode": "official-user-hooks",
        "configuration_status": "active",
        "host_detected": detected,
    }


def _managed_ide_absent_result(configured: bool, detected: bool) -> dict[str, object]:
    if configured or detected:
        return {
            "status": "degraded",
            "message": "Local host is missing its managed LLM-Wiki hooks.",
            "configuration_status": "absent",
            "host_detected": detected,
        }
    return {
        "status": "skipped",
        "message": "Optional local host not detected.",
        "configuration_status": "absent",
        "host_detected": False,
    }


def _managed_ide_host_result(root: Path, home: Path, name: str) -> dict[str, object]:
    detected = _managed_ide_detected(home, name)
    try:
        resource = _managed_ide_resource(root, home, name)
        destination = Path(resource.locator)
        configured = destination.exists() or destination.is_symlink()
        active = resource.read_owned() == resource.desired
    except (InstallControlError, OSError, UnicodeError, ValueError):
        return _managed_ide_conflict_result(detected)
    if active:
        return _managed_ide_active_result(detected)
    return _managed_ide_absent_result(configured, detected)


def _required_host_config(
    config: tuple[Path, list[tuple[Path, tuple[str, ...]]]] | None,
) -> tuple[Path, list[tuple[Path, tuple[str, ...]]]]:
    if config is None:
        raise ValueError("missing integration host configuration")
    return config


def _integration_host_result(
    root: Path,
    home: Path,
    name: str,
    config: tuple[Path, list[tuple[Path, tuple[str, ...]]]] | None,
    deadline: float,
) -> dict[str, object]:
    if name == "codex":
        return _codex_host_result(root, home, deadline)
    if name in {"cursor", "antigravity"}:
        return _managed_ide_host_result(root, home, name)
    return _generic_host_result(*_required_host_config(config))


def _integration_hosts(root: Path, home: Path, deadline: float) -> dict[str, dict[str, object]]:
    configs = _integration_host_configs(home)
    names = ("claude", "opencode", "codex", "cursor", "antigravity")
    return {
        name: _integration_host_result(root, home, name, configs.get(name), deadline)
        for name in names
    }


def _integration_summary(
    source_details: Mapping[str, bool], hosts: Mapping[str, Mapping[str, object]]
) -> tuple[str, str]:
    missing_sources = sum(not available for available in source_details.values())
    if missing_sources:
        return "error", f"{missing_sources} integration source adapter(s) are missing."
    configured_missing = sum(host.get("status") == "degraded" for host in hosts.values())
    if configured_missing:
        return "degraded", f"{configured_missing} installed host(s) lack integration config."
    return "ok", "Integration sources are available; optional hosts were checked."


def _integration_check(root: Path, home: Path, *, deadline: float = float("inf")) -> dict:
    source_details = {name: path.is_file() for name, path in _integration_sources(root).items()}
    hosts = _integration_hosts(root, home, deadline)
    status, message = _integration_summary(source_details, hosts)
    return _result("integrations", status, message, {"sources": source_details, "hosts": hosts})


def _repair_runtime(state_root: Path, repaired: list[dict]) -> None:
    for relative in RUNTIME_DIRECTORIES:
        path = state_root / relative
        kind, _ = _safe_kind(path, state_root)
        if kind not in {"missing", "directory"}:
            raise OSError(f"unsafe runtime path: {relative}")
        if kind == "missing":
            path.mkdir(parents=True, exist_ok=True)
            repaired.append({"action": "create_runtime_directory", "directory": relative})


def _windows_process_state(pid: int) -> str:
    """Ask the kernel directly; a missing process is dead, anything else unknown."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        handle = open_process(0x1000, False, pid)
        if not handle:
            return _windows_open_failure_state(ctypes.get_last_error())
        try:
            return _windows_exit_code_state(ctypes, wintypes, get_exit_code, handle)
        finally:
            close_handle(handle)
    except (AttributeError, OSError, OverflowError, ValueError):
        return "unknown"


def _windows_open_failure_state(last_error: int) -> str:
    if last_error in {87, 1168}:
        return "dead"
    return "unknown"


def _windows_exit_code_state(ctypes, wintypes, get_exit_code, handle) -> str:
    exit_code = wintypes.DWORD()
    if not get_exit_code(handle, ctypes.byref(exit_code)):
        return "unknown"
    if exit_code.value == 259:
        return "alive"
    return "dead"


def _os_error_process_state(exc: OSError) -> str:
    if exc.errno == errno.ESRCH:
        return "dead"
    return "unknown"


def _posix_process_state(pid: int) -> str:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "unknown"
    except OSError as exc:
        return _os_error_process_state(exc)
    except (OverflowError, ValueError):
        return "unknown"
    return "alive"


def _lsp_pid_state(pid: int) -> str:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return "unknown"
    if sys.platform == "win32":
        return _windows_process_state(pid)
    return _posix_process_state(pid)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        process_query = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(process_query, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, OverflowError, ValueError):
        return False


def _lock_metadata(pid: int, token: str, now: datetime) -> bytes:
    return json.dumps(
        {
            "lock_pid": pid,
            "lock_token": token,
            "lock_acquired_at": now.isoformat(),
        }
    ).encode("utf-8")


def _create_owned_lock(path: Path, token: str, now: datetime) -> bool:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    try:
        os.write(fd, _lock_metadata(os.getpid(), token, now))
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(fd)
    return True


def _lock_file_nonblocking(fd: int) -> bool:
    if sys.platform == "win32":
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock_file(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _read_lock_fd(fd: int) -> tuple[dict | None, os.stat_result | None]:
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_LOCK_BYTES:
            return None, None
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, MAX_LOCK_BYTES + 1)
        if len(raw) > MAX_LOCK_BYTES:
            return None, None
        value = json.loads(raw.decode("utf-8"))
        return (value, opened) if isinstance(value, dict) else (None, None)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, None


def _open_existing_lock(path: Path, root: Path) -> int | None:
    if _safe_kind(path, root)[0] != "regular":
        return None
    if sys.platform != "win32":
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            return os.open(path, flags)
        except OSError:
            return None

    import ctypes
    import msvcrt

    generic_read_write = 0x80000000 | 0x40000000
    share_read_write_delete = 0x1 | 0x2 | 0x4
    open_existing = 3
    file_attribute_normal = 0x80
    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        generic_read_write,
        share_read_write_delete,
        None,
        open_existing,
        file_attribute_normal,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        return None
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDWR | getattr(os, "O_BINARY", 0))
    except OSError:
        ctypes.windll.kernel32.CloseHandle(handle)
        return None


def _lock_acquired_at(existing: dict) -> datetime | None:
    try:
        acquired = datetime.fromisoformat(str(existing.get("lock_acquired_at", "")))
    except (TypeError, ValueError):
        return None
    if acquired.tzinfo is None:
        acquired = acquired.replace(tzinfo=timezone.utc)
    return acquired.astimezone(timezone.utc)


def _dead_lock_pid(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    return not _pid_alive(pid)


def _known_lock_owner(existing: dict) -> str | None:
    if not _dead_lock_pid(existing.get("lock_pid")):
        return None
    old_token = existing.get("lock_token")
    if not isinstance(old_token, str) or not old_token:
        return None
    return old_token


def _stale_lock_owner(existing: dict, now: datetime) -> str | None:
    """The token of a lock whose owner is both timed out and gone."""
    acquired = _lock_acquired_at(existing)
    if acquired is None:
        return None
    if (now - acquired).total_seconds() <= LOCK_STALE_SECONDS:
        return None
    return _known_lock_owner(existing)


def _lock_unchanged(fd: int, path: Path, old_token: str, opened_stat) -> bool:
    current_value, current_opened_stat = _read_lock_fd(fd)
    if current_value is None or current_opened_stat is None:
        return False
    if current_value.get("lock_token") != old_token:
        return False
    try:
        current_path_stat = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return os.path.samestat(opened_stat, current_path_stat)


def _remove_path_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _replace_stale_lock(path: Path, token: str, now: datetime) -> str | None:
    quarantine = path.with_name(f"{path.name}.stale-{token}")
    try:
        path.rename(quarantine)
    except OSError:
        return None
    try:
        if _create_owned_lock(path, token, now):
            return token
        return None
    finally:
        _remove_path_quietly(quarantine)


def _readable_lock(existing: object, opened_stat: object) -> bool:
    return existing is not None and opened_stat is not None


def _take_over_stale_lock(
    fd: int, path: Path, token: str, now: datetime
) -> str | None:
    if not _lock_file_nonblocking(fd):
        return None
    existing, opened_stat = _read_lock_fd(fd)
    if not _readable_lock(existing, opened_stat):
        return None
    old_token = _stale_lock_owner(existing, now)
    if old_token is None:
        return None
    if not _lock_unchanged(fd, path, old_token, opened_stat):
        return None
    return _replace_stale_lock(path, token, now)


def _acquire_lock(path: Path, root: Path, now: datetime) -> str | None:
    token = secrets.token_hex(16)
    if _create_owned_lock(path, token, now):
        return token
    fd = _open_existing_lock(path, root)
    if fd is None:
        return None
    try:
        return _take_over_stale_lock(fd, path, token, now)
    finally:
        _unlock_file(fd)
        os.close(fd)


def _release_lock(path: Path, root: Path, token: str) -> None:
    current, problem = _read_bounded_json(path, root, max_bytes=MAX_LOCK_BYTES)
    if not problem and current is not None and current.get("lock_token") == token:
        try:
            path.unlink()
        except OSError:
            pass


def _abandoned_lease(task: dict, now: datetime) -> bool:
    """A lease is recoverable only when it expired and its owner is gone."""
    stale, owned = _lease_state(task, now)
    if not stale or not owned:
        return False
    return not _pid_alive(task.get("lease_pid"))


def _restore_lease_as_task(lease: Path) -> bool:
    try:
        os.link(lease, lease.with_suffix(".json"), follow_symlinks=False)
        lease.unlink()
    except (FileExistsError, OSError):
        return False
    return True


def _recoverable_lease(entry: os.DirEntry, queue: Path, now: datetime) -> Path | None:
    if not entry.name.endswith(".processing"):
        return None
    lease = Path(entry.path)
    task, problem = _read_bounded_json(lease, queue)
    if problem or task is None:
        return None
    if not _abandoned_lease(task, now):
        return None
    return lease


def _recover_stale_leases(queue: Path, now: datetime) -> int:
    recovered = 0
    with os.scandir(queue) as entries:
        for number, entry in enumerate(entries):
            if number >= MAX_QUEUE_FILES:
                return recovered
            lease = _recoverable_lease(entry, queue, now)
            if lease is not None and _restore_lease_as_task(lease):
                recovered += 1
    return recovered


def _repair_leases(state_root: Path, now: datetime, repaired: list[dict]) -> bool:
    queue = state_root / "run" / "queue"
    kind = _safe_kind(queue, state_root)[0]
    if kind == "missing":
        return True
    if kind != "directory":
        raise OSError("unsafe queue directory")
    lock = queue / ".doctor-recovery.lock"
    lock_token = _acquire_lock(lock, queue, now)
    if lock_token is None:
        return False
    try:
        recovered = _recover_stale_leases(queue, now)
    finally:
        _release_lock(lock, queue, lock_token)
    if recovered:
        repaired.append({"action": "recover_stale_lease", "count": recovered})
    return True


def _require_real_directory(component: Path) -> None:
    try:
        component_info = component.lstat()
    except OSError as exc:
        raise OSError("unsafe knowledge path") from exc
    if stat.S_ISLNK(component_info.st_mode):
        raise OSError("unsafe knowledge path")
    if not stat.S_ISDIR(component_info.st_mode):
        raise OSError("unsafe knowledge path")


def _contained_notes_directory(root: Path) -> Path:
    """The notes directory, proven to be a real directory inside the vault."""
    knowledge_path = root / "knowledge"
    notes_path = knowledge_path / "notes"
    _require_real_directory(knowledge_path)
    _require_real_directory(notes_path)
    notes = notes_path.resolve()
    try:
        notes.relative_to(root.resolve())
    except ValueError as exc:
        raise OSError("unsafe knowledge path") from exc
    return notes


def _require_rebuild_continues(deadline: float, cancelled) -> None:
    if _deadline_reached(deadline) or bool(cancelled and cancelled()):
        raise TimeoutError("index rebuild cancelled or deadline reached")


def _rebuildable_pages(notes: Path, deadline: float, cancelled) -> list[Path]:
    pages = []
    for page in sorted(notes.rglob("*.md")):
        _require_rebuild_continues(deadline, cancelled)
        if _safe_kind(page, notes)[0] == "regular":
            pages.append(page)
    return pages


def _rebuild_index(
    root: Path,
    state_root: Path,
    *,
    deadline: float = float("inf"),
    cancelled=None,
) -> None:
    import search_memory

    previous = (
        search_memory.ROOT,
        search_memory.STATE_ROOT,
        search_memory.INDEX_DIR,
        search_memory.INDEX_FILE,
        search_memory.INDEX_MANIFEST,
        search_memory.KNOWLEDGE_DIR,
        search_memory.WIKI_DIR,
    )
    try:
        search_memory.ROOT = root
        search_memory.STATE_ROOT = state_root
        search_memory.INDEX_DIR = state_root / "cache"
        search_memory.INDEX_FILE = search_memory.INDEX_DIR / "index.sqlite"
        search_memory.INDEX_MANIFEST = search_memory.INDEX_DIR / ".paths-manifest"
        search_memory.KNOWLEDGE_DIR = root / "knowledge" / "notes"
        search_memory.WIKI_DIR = search_memory.KNOWLEDGE_DIR
        notes = _contained_notes_directory(root)
        pages = _rebuildable_pages(notes, deadline, cancelled)
        _require_rebuild_continues(deadline, cancelled)
        search_memory._build_index(pages)  # noqa: SLF001
    finally:
        (
            search_memory.ROOT,
            search_memory.STATE_ROOT,
            search_memory.INDEX_DIR,
            search_memory.INDEX_FILE,
            search_memory.INDEX_MANIFEST,
            search_memory.KNOWLEDGE_DIR,
            search_memory.WIKI_DIR,
        ) = previous


def _ensure_maintenance_schema(database: sqlite3.Connection) -> None:
    database.execute(
        """CREATE TABLE IF NOT EXISTS maintenance_owners (
               owner_name TEXT PRIMARY KEY,
               owner_token TEXT NOT NULL,
               process_id INTEGER NOT NULL,
               acquired_at TEXT NOT NULL,
               heartbeat_at TEXT,
               expires_at TEXT,
               fencing_epoch INTEGER NOT NULL DEFAULT 1
           )"""
    )
    columns = _columns(database, "maintenance_owners")
    for name, declaration in (
        ("heartbeat_at", "TEXT"),
        ("expires_at", "TEXT"),
        ("fencing_epoch", "INTEGER NOT NULL DEFAULT 1"),
    ):
        if name not in columns:
            database.execute(f"ALTER TABLE maintenance_owners ADD COLUMN {name} {declaration}")


def _acquire_maintenance_owner(
    root: Path, state_root: Path, now: datetime
) -> tuple[Any, dict[str, object]] | None:
    from markdown_transaction import MarkdownCoordinator

    coordinator = MarkdownCoordinator(root, state_root)
    token = secrets.token_hex(16)
    expires = now + timedelta(seconds=MAINTENANCE_LEASE_SECONDS)
    with coordinator._connect() as database:
        database.execute("BEGIN IMMEDIATE")
        _ensure_maintenance_schema(database)
        row = database.execute(
            "SELECT * FROM maintenance_owners WHERE owner_name='doctor'"
        ).fetchone()
        epoch = 1
        if row is not None:
            epoch = int(row["fencing_epoch"] or 0) + 1
            if _live_owner(row, now, pid_column="process_id"):
                database.rollback()
                return None
        database.execute(
            """INSERT INTO maintenance_owners(
                   owner_name,owner_token,process_id,acquired_at,heartbeat_at,
                   expires_at,fencing_epoch
               ) VALUES('doctor',?,?,?,?,?,?)
               ON CONFLICT(owner_name) DO UPDATE SET
                   owner_token=excluded.owner_token,
                   process_id=excluded.process_id,
                   acquired_at=excluded.acquired_at,
                   heartbeat_at=excluded.heartbeat_at,
                   expires_at=excluded.expires_at,
                   fencing_epoch=excluded.fencing_epoch""",
            (
                token,
                os.getpid(),
                now.isoformat(),
                now.isoformat(),
                expires.isoformat(),
                epoch,
            ),
        )
        database.commit()
    return coordinator, {"token": token, "epoch": epoch}


def _heartbeat_maintenance_owner(
    coordinator: Any, lease: dict[str, object], now: datetime | None = None
) -> None:
    heartbeat = _as_utc(now)
    expires = heartbeat + timedelta(seconds=MAINTENANCE_LEASE_SECONDS)
    with coordinator._connect() as database:
        database.execute("BEGIN IMMEDIATE")
        changed = database.execute(
            """UPDATE maintenance_owners SET heartbeat_at=?,expires_at=?
               WHERE owner_name='doctor' AND owner_token=? AND fencing_epoch=?""",
            (
                heartbeat.isoformat(),
                expires.isoformat(),
                lease["token"],
                lease["epoch"],
            ),
        ).rowcount
        if changed != 1:
            database.rollback()
            raise RuntimeError("maintenance_owner_fence_lost")
        database.commit()


def _require_maintenance_owner(coordinator: Any, lease: dict[str, object]) -> None:
    with coordinator._connect() as database:
        row = database.execute(
            "SELECT owner_token,process_id,fencing_epoch FROM maintenance_owners "
            "WHERE owner_name='doctor'"
        ).fetchone()
    if (
        row is None
        or row["owner_token"] != lease["token"]
        or row["fencing_epoch"] != lease["epoch"]
        or row["process_id"] != os.getpid()
    ):
        raise RuntimeError("maintenance_owner_fence_lost")


def _release_maintenance_owner(coordinator: Any, lease: dict[str, object]) -> None:
    with coordinator._connect() as database:
        database.execute("BEGIN IMMEDIATE")
        released_at = datetime.min.replace(tzinfo=timezone.utc).isoformat()
        changed = database.execute(
            """UPDATE maintenance_owners
               SET owner_token='',process_id=0,heartbeat_at=?,expires_at=?
               WHERE owner_name='doctor' AND owner_token=? AND fencing_epoch=?""",
            (
                released_at,
                released_at,
                lease["token"],
                lease["epoch"],
            ),
        ).rowcount
        if changed != 1:
            database.rollback()
            raise RuntimeError("maintenance_owner_fence_lost")
        database.commit()


class _MaintenanceHeartbeat:
    """Keep one fenced maintenance owner live through cancellable repair work."""

    def __init__(
        self,
        coordinator: Any,
        lease: dict[str, object],
        *,
        deadline: float,
    ) -> None:
        self.coordinator = coordinator
        self.lease = lease
        self.deadline = deadline
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _MaintenanceHeartbeat:
        self.check()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name="llm-wiki-doctor-heartbeat",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, MAINTENANCE_HEARTBEAT_SECONDS * 2))
        _release_maintenance_owner(self.coordinator, self.lease)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(MAINTENANCE_HEARTBEAT_SECONDS):
            try:
                _heartbeat_maintenance_owner(self.coordinator, self.lease)
            except Exception:  # noqa: BLE001 - observed by the foreground fence check
                self._lost.set()
                return

    def cancelled(self) -> bool:
        return self._lost.is_set() or _deadline_reached(self.deadline)

    def check(self) -> None:
        if self.cancelled():
            raise TimeoutError("maintenance deadline or heartbeat fence reached")
        _require_maintenance_owner(self.coordinator, self.lease)

    def run(self, operation, /, *args, **kwargs):
        self.check()
        result = operation(*args, **kwargs)
        self.check()
        return result

    def cleanup(self, operation, /, *args, **kwargs):
        _require_maintenance_owner(self.coordinator, self.lease)
        result = operation(*args, **kwargs)
        _require_maintenance_owner(self.coordinator, self.lease)
        return result


def _generation_cache_absent(state_root: Path, graph_root: Path) -> bool:
    if _safe_kind(graph_root / "catalog.sqlite3", state_root)[0] != "missing":
        return False
    return _safe_kind(graph_root / "generations", state_root)[0] == "missing"


def _active_pointer_value(catalog: object) -> str | None:
    with closing(
        catalog._readonly()  # noqa: SLF001 - repair needs pointer comparison
    ) as database:
        return _active_pointer(database)


def _record_generation_recovery(
    catalog: object, deadline: float, repaired: list[dict], active_before: str | None
) -> None:
    recovered = catalog.recover_orphans(deadline=deadline)
    if recovered:
        repaired.append(
            {"action": "recover_generation_orphans", "count": len(recovered)}
        )
    active_manifest = catalog.get_active(deadline=deadline)
    active_after = _active_generation_id(active_manifest)
    if active_after != active_before:
        repaired.append(
            {
                "action": "fallback_generation",
                "from": active_before,
                "to": active_after,
            }
        )


def _registered_generation_ids(catalog: object, generation_catalog) -> set[str]:
    with closing(catalog._readonly()) as database:  # noqa: SLF001 - bounded repair
        rows = database.execute(
            "SELECT generation_id FROM generations LIMIT ?",
            (generation_catalog.MAX_GENERATIONS + 1,),
        ).fetchall()
    if len(rows) > generation_catalog.MAX_GENERATIONS:
        raise ValueError("generation catalog exceeds cleanup bound")
    return {str(row[0]) for row in rows}


def _cleanup_stop_reached(deadline: float, cancelled) -> bool:
    return bool(cancelled and cancelled()) or _deadline_reached(deadline)


def _skip_generation_child(entry: os.DirEntry, registered: set[str]) -> bool:
    return entry.name in registered or not entry.is_dir(follow_symlinks=False)


def _removable_generation_orphan(
    path: Path,
    entry_name: str,
    state_root: Path,
    catalog: object,
    generation_catalog,
    deadline: float,
    cancelled,
) -> bool:
    """True only for an unregistered child that fails validation in place."""
    try:
        generation_catalog._generation_id(entry_name)  # noqa: SLF001
        if generation_catalog._is_link_or_reparse(path):  # noqa: SLF001
            return False
        generation_catalog._validate_generation(  # noqa: SLF001
            path,
            state_root,
            deadline=deadline,
            cancelled=cancelled,
        )
    except TimeoutError:
        raise
    except (FileNotFoundError, OSError, PermissionError, TypeError, ValueError):
        parent = path.parent.resolve(strict=True)
        return parent == catalog.generations_path.resolve(strict=True)
    return False


def _cleanup_generation_orphans(
    catalog: object,
    generation_catalog,
    state_root: Path,
    registered: set[str],
    deadline: float,
    cancelled,
) -> int:
    children = generation_catalog._bounded_scandir(  # noqa: SLF001
        catalog.generations_path,
        generation_catalog.MAX_GENERATION_CHILDREN,
        "generation child count exceeds cleanup bound",
        deadline=deadline,
        cancelled=cancelled,
    )
    removed = 0
    for entry in children:
        if _cleanup_stop_reached(deadline, cancelled):
            raise TimeoutError("generation cleanup deadline reached")
        if _skip_generation_child(entry, registered):
            continue
        path = Path(entry.path)
        if _removable_generation_orphan(
            path,
            entry.name,
            state_root,
            catalog,
            generation_catalog,
            deadline,
            cancelled,
        ):
            shutil.rmtree(path)
            removed += 1
    return removed


def _repair_generation_catalog(
    root: Path,
    state_root: Path,
    *,
    deadline: float,
    cancelled,
    repaired: list[dict],
) -> None:
    """Recover valid generations and remove only invalid unregistered partials."""
    del root
    import generation_catalog

    graph_root = state_root / "cache" / "evidence-graph"
    if _generation_cache_absent(state_root, graph_root):
        return
    catalog = generation_catalog.GenerationCatalog(state_root)
    active_before = _active_pointer_value(catalog)
    _record_generation_recovery(catalog, deadline, repaired, active_before)
    registered = _registered_generation_ids(catalog, generation_catalog)
    removed = _cleanup_generation_orphans(
        catalog, generation_catalog, state_root, registered, deadline, cancelled
    )
    if removed:
        repaired.append({"action": "cleanup_generation_orphans", "count": removed})


class _PartitionState(NamedTuple):
    grouped: dict
    occurrence_sources: dict
    record_sources: dict
    node_references: dict
    dependencies: dict
    workspace_sensitive: set


def _new_partition_state(source_ids: tuple[str, ...]) -> _PartitionState:
    grouped = {
        source_id: {
            "nodes": [],
            "occurrences": [],
            "assertions": [],
            "evidence": [],
            "observations": [],
            "dependencies": [],
        }
        for source_id in source_ids
    }
    return _PartitionState(
        grouped=grouped,
        occurrence_sources={},
        record_sources={},
        node_references={},
        dependencies={source_id: set() for source_id in source_ids},
        workspace_sensitive=set(),
    )


def _record_owner(state: _PartitionState, record_id: object, source_id: str) -> None:
    if record_id is None:
        return
    state.record_sources[str(record_id)] = source_id


def _reference_node(state: _PartitionState, node_id: object, owner: str) -> None:
    if node_id is None:
        return
    state.node_references.setdefault(str(node_id), set()).add(owner)


def _partition_occurrences(result, state: _PartitionState, check_stop) -> None:
    for occurrence in result.occurrences:
        check_stop()
        source_id = str(occurrence["source_id"])
        node_id = str(occurrence["node_id"])
        state.occurrence_sources.setdefault(node_id, set()).add(source_id)
        state.node_references.setdefault(node_id, set()).add(source_id)
        state.grouped[source_id]["occurrences"].append(occurrence)


def _partition_evidence(result, state: _PartitionState, check_stop) -> None:
    for evidence in result.evidence:
        check_stop()
        source_id = str(evidence["source_id"])
        state.grouped[source_id]["evidence"].append(evidence)
        _record_owner(state, evidence.get("assertion_id"), source_id)
        _record_owner(state, evidence.get("observation_id"), source_id)


def _partition_assertions(result, state: _PartitionState, check_stop) -> None:
    for assertion in result.assertions:
        check_stop()
        owner = state.record_sources[str(assertion["assertion_id"])]
        state.grouped[owner]["assertions"].append(assertion)
        _reference_node(state, assertion["source_node_id"], owner)
        target = assertion.get("target_node_id")
        if target is None:
            continue
        _reference_node(state, target, owner)
        state.dependencies[owner].update(
            state.occurrence_sources.get(str(target), ())
        )


def _workspace_sensitive_observation(
    observation: dict, observation_dependencies: dict
) -> bool:
    if observation["reason"] not in {"missing_dependency", "unresolved_reference"}:
        return False
    return str(observation["observation_id"]) not in observation_dependencies


def _partition_observations(result, state: _PartitionState, check_stop) -> None:
    observation_dependencies = getattr(result, "observation_source_dependencies", {})
    for observation in result.observations:
        check_stop()
        owner = state.record_sources[str(observation["observation_id"])]
        state.grouped[owner]["observations"].append(observation)
        _reference_node(state, observation.get("source_node_id"), owner)
        if _workspace_sensitive_observation(observation, observation_dependencies):
            state.workspace_sensitive.add(owner)


def _partition_source_dependencies(result, state: _PartitionState, check_stop) -> None:
    declared = getattr(result, "observation_source_dependencies", {})
    for observation_id, candidate_sources in declared.items():
        check_stop()
        owner = state.record_sources[str(observation_id)]
        state.dependencies[owner].update(candidate_sources)


def _dependency_owners(dependency: dict, state: _PartitionState) -> tuple[str, ...]:
    owner = dependency.get("source_id")
    if owner is not None:
        return (str(owner),)
    node_id = str(dependency["dependent_node_id"])
    return tuple(sorted(state.occurrence_sources.get(node_id, ())))


def _partition_dependencies(result, state: _PartitionState, check_stop) -> None:
    for dependency in getattr(result, "dependencies", ()):
        check_stop()
        for source_id in _dependency_owners(dependency, state):
            state.grouped[source_id]["dependencies"].append(dependency)


def _partition_nodes(
    result, state: _PartitionState, check_stop, fallback_owner: str
) -> None:
    for node in result.nodes:
        check_stop()
        node_id = str(node["node_id"])
        owners = state.occurrence_sources.get(node_id)
        if not owners:
            owners = state.node_references.get(node_id, {fallback_owner})
        for source_id in sorted(owners):
            state.grouped[source_id]["nodes"].append(node)


def _drop_self_dependencies(
    source_ids: tuple[str, ...], state: _PartitionState, check_stop
) -> None:
    for source_id in source_ids:
        check_stop()
        state.dependencies[source_id].discard(source_id)


def _source_partitions(
    source_ids: tuple[str, ...],
    state: _PartitionState,
    check_stop,
    source_extraction,
) -> dict:
    partitions = {}
    for source_id in source_ids:
        check_stop()
        records = state.grouped[source_id]
        partitions[source_id] = source_extraction(
            nodes=tuple(records["nodes"]),
            occurrences=tuple(records["occurrences"]),
            assertions=tuple(records["assertions"]),
            evidence=tuple(records["evidence"]),
            observations=tuple(records["observations"]),
            dependencies=tuple(records["dependencies"]),
            source_dependencies=tuple(sorted(state.dependencies[source_id])),
            workspace_sensitive=source_id in state.workspace_sensitive,
        )
    return partitions


def _partition_code_extraction(
    result,
    code_sources,
    *,
    deadline: float | None = None,
    cancelled=None,
):
    """Partition one multi-source extraction by the source proving each record."""
    from evidence_graph_builder import SourceExtraction

    source_ids = tuple(source.record.logical_id for source in code_sources)
    state = _new_partition_state(source_ids)

    def check_stop() -> None:
        if cancelled is not None and cancelled():
            raise TimeoutError("workspace extraction partition cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("workspace extraction partition deadline reached")

    check_stop()
    _partition_occurrences(result, state, check_stop)
    _partition_evidence(result, state, check_stop)
    _partition_assertions(result, state, check_stop)
    _partition_observations(result, state, check_stop)
    _partition_source_dependencies(result, state, check_stop)
    _partition_dependencies(result, state, check_stop)
    _partition_nodes(result, state, check_stop, min(source_ids))
    _drop_self_dependencies(source_ids, state, check_stop)
    return _source_partitions(source_ids, state, check_stop, SourceExtraction)


def _generation_source_extractor(snapshot, repository_id: str):
    """Return the incremental builder adapter for one immutable snapshot."""
    from evidence_graph_builder import SourceExtraction

    by_id = {source.record.logical_id: source for source in snapshot.sources}
    code_sources = tuple(
        sorted(
            (
                source
                for source in snapshot.sources
                if not source.record.relative_path.startswith("knowledge/")
            ),
            key=lambda source: (
                source.record.logical_id,
                source.record.relative_path,
                source.record.language or "",
            ),
        )
    )
    knowledge_sources = tuple(
        source
        for source in snapshot.sources
        if source.record.relative_path.startswith("knowledge/")
        and not source.record.relative_path.startswith("knowledge/projects/")
    )
    code_partitions = None
    knowledge_partitions = None

    def extract(source, content, *, sources, source_bytes, deadline, cancelled):
        nonlocal code_partitions, knowledge_partitions
        del sources
        captured = by_id[str(source["source_id"])]
        if captured.content != content:
            raise ValueError("incremental extraction bytes differ from snapshot")
        path = captured.record.relative_path
        if path.startswith("knowledge/projects/"):
            from project_extractor import extract_projects

            result = extract_projects((captured,), deadline=deadline, cancelled=cancelled)
        elif path.startswith("knowledge/"):
            from knowledge_extractor import extract_knowledge

            if knowledge_partitions is None:
                if any(
                    item.content != source_bytes[item.record.logical_id]
                    for item in knowledge_sources
                ):
                    raise ValueError("knowledge extraction bytes differ from snapshot")
                workspace_result = extract_knowledge(
                    knowledge_sources,
                    deadline=deadline,
                    cancelled=cancelled,
                )
                knowledge_partitions = _partition_code_extraction(
                    workspace_result,
                    knowledge_sources,
                    deadline=deadline,
                    cancelled=cancelled,
                )
            result = knowledge_partitions[captured.record.logical_id]
        else:
            from code_extractor import extract_code

            if code_partitions is None:
                if any(
                    item.content != source_bytes[item.record.logical_id] for item in code_sources
                ):
                    raise ValueError("workspace extraction bytes differ from snapshot")
                workspace_result = extract_code(
                    code_sources,
                    repository_id=repository_id,
                    deadline=deadline,
                    cancelled=cancelled,
                )
                code_partitions = _partition_code_extraction(
                    workspace_result,
                    code_sources,
                    deadline=deadline,
                    cancelled=cancelled,
                )
            result = code_partitions[captured.record.logical_id]
        digest = hashlib.sha256(content).hexdigest()
        fingerprints = {
            key: hashlib.sha256(f"{key}:{digest}".encode("ascii")).hexdigest()
            for key in (
                "exports",
                "imports",
                "signatures",
                "aliases",
                "project_metadata",
            )
        }
        return SourceExtraction(
            nodes=tuple(result.nodes),
            occurrences=tuple(result.occurrences),
            assertions=tuple(result.assertions),
            evidence=tuple(result.evidence),
            observations=tuple(result.observations),
            dependencies=tuple(getattr(result, "dependencies", ())),
            source_dependencies=tuple(getattr(result, "source_dependencies", ())),
            workspace_sensitive=bool(getattr(result, "workspace_sensitive", False)),
            invalidation_fingerprints=fingerprints,
        )

    return extract


def _corpus_policy(snapshot: object) -> dict:
    return {
        "daily_paths": list(snapshot.policy.daily_paths),
        "code_roots": list(snapshot.policy.code_roots),
        "include_historical": snapshot.policy.include_historical,
        "as_of": snapshot.policy.as_of,
    }


def _workspace_manifest_sha256(snapshot: object) -> str:
    """Identity of the non-knowledge sources, which decides graph reuse."""
    from reliable_memory import canonical_json_bytes

    membership = sorted(
        [
            source.record.logical_id,
            source.record.relative_path,
            source.record.language,
        ]
        for source in snapshot.sources
        if not source.record.relative_path.startswith("knowledge/")
    )
    return hashlib.sha256(canonical_json_bytes(membership)).hexdigest()


def _parent_workspace_manifest(
    catalog: object, parent_id: str | None, deadline: float, cancelled
) -> str | None:
    if parent_id is None:
        return None
    from evidence_graph_builder import _load_incremental_manifest

    parent_incremental, _parent_generation = _load_incremental_manifest(  # noqa: SLF001
        catalog,
        parent_id,
        deadline=deadline,
        cancelled=cancelled,
    )
    if parent_incremental is None:
        return None
    return parent_incremental["reuse_config"].get("workspace_manifest_sha256")


def _parent_matches_identity(
    parent: dict, repository_scope: object, snapshot: object, extractor_version: str
) -> bool:
    if parent.get("schema_version") != "corpus-generation/v2":
        return False
    if parent.get("repository_scope") != repository_scope.as_dict():
        return False
    if parent.get("collector_version") != snapshot.collector_version:
        return False
    if parent.get("extractor_version") != snapshot.extractor_version:
        return False
    return parent.get("graph_extractor_version") == extractor_version


def _parent_is_current(
    parent: dict | None,
    force_rebuild: bool,
    repository_scope: object,
    snapshot: object,
    extractor_version: str,
    parent_workspace_sha256: str | None,
    workspace_sha256: str,
) -> bool:
    """True when the active generation already describes the live sources."""
    if parent is None or force_rebuild:
        return False
    if not _parent_matches_identity(
        parent, repository_scope, snapshot, extractor_version
    ):
        return False
    if parent.get("source_manifest_sha256") != snapshot.corpus_sha256:
        return False
    return parent_workspace_sha256 == workspace_sha256


def _generation_source_rows(snapshot: object) -> list[dict]:
    return [
        {
            "source_id": source.record.logical_id,
            "relative_path": source.record.relative_path,
            "sha256": source.record.sha256,
            "size": source.record.size,
            "media_type": source.record.media_type,
            "language": source.record.language,
            "git_oid": source.record.git_oid,
        }
        for source in snapshot.sources
    ]


def _generation_source_bytes(snapshot: object) -> dict:
    return {source.record.logical_id: source.content for source in snapshot.sources}


def _approved_code_roots(root: Path, approved: set[str]) -> tuple[str, ...]:
    return tuple(
        relative for relative in sorted(approved) if (root / relative).is_dir()
    )


def _active_generation_id(parent: dict | None) -> str | None:
    if parent is None:
        return None
    return str(parent["generation_id"])


def _reuse_parent_id(force_rebuild: bool, parent_id: str | None) -> str | None:
    if force_rebuild:
        return None
    return parent_id


def _fresh_generation_id(catalog: object) -> str:
    while True:
        generation_id = f"generation-{time.time_ns():x}-{secrets.token_hex(4)}"
        if not (catalog.generations_path / generation_id).exists():
            return generation_id


def _generation_build_result(built: object, snapshot: object) -> dict:
    if not built.activated:
        return {
            "status": "deferred",
            "generation_id": built.generation_id,
            "sources": len(snapshot.sources),
            "partial": True,
            "reason": "activation_race",
        }
    return {
        "status": "built",
        "generation_id": built.generation_id,
        "sources": len(snapshot.sources),
        "rebuilt_sources": len(built.rebuilt_sources),
        "reused_sources": len(built.reused_sources),
        "partial": False,
    }


def _build_or_refresh_generation(
    root: Path,
    state_root: Path,
    *,
    deadline: float,
    cancelled,
    max_sources: int,
    force_rebuild: bool,
    coordinator: object | None = None,
) -> dict:
    from corpus_snapshot import APPROVED_CODE_ROOTS, collect_corpus
    from evidence_graph_builder import (
        GRAPH_SCHEMA_VERSION,
        IncrementalReuseConfig,
        build_incremental_generation,
    )
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    repository_scope = resolve_repository_scope(
        root, deadline=deadline, cancelled=cancelled
    )
    extractor_version = _maintenance_extractor_identity()
    snapshot = collect_corpus(
        root,
        code_roots=_approved_code_roots(root, APPROVED_CODE_ROOTS),
        max_files=max_sources,
        deadline=deadline,
    )
    if len(snapshot.sources) > max_sources:
        raise ValueError("corpus source limit exceeded")

    catalog = GenerationCatalog(state_root)
    parent = catalog.get_active(deadline=deadline)
    parent_id = _active_generation_id(parent)
    workspace_sha256 = _workspace_manifest_sha256(snapshot)
    parent_workspace_sha256 = _parent_workspace_manifest(
        catalog, parent_id, deadline, cancelled
    )
    if _parent_is_current(
        parent,
        force_rebuild,
        repository_scope,
        snapshot,
        extractor_version,
        parent_workspace_sha256,
        workspace_sha256,
    ):
        return {
            "status": "current",
            "generation_id": parent_id,
            "sources": len(snapshot.sources),
            "partial": False,
        }

    config = IncrementalReuseConfig(
        extractor_version=extractor_version,
        grammar_version="builtin-grammars/v1",
        compiler_version=f"python-{sys.version_info.major}.{sys.version_info.minor}",
        resolver_config_sha256=hashlib.sha256(
            b"llm-wiki-maintenance-resolver/v1"
        ).hexdigest(),
        schema_version=GRAPH_SCHEMA_VERSION,
        workspace_manifest_sha256=workspace_sha256,
    )
    built = build_incremental_generation(
        catalog,
        sources=_generation_source_rows(snapshot),
        source_bytes=_generation_source_bytes(snapshot),
        extractor=_generation_source_extractor(
            snapshot, repository_scope.repository_id
        ),
        reuse_config=config,
        generation_id=_fresh_generation_id(catalog),
        parent_generation_id=_reuse_parent_id(force_rebuild, parent_id),
        policy=_corpus_policy(snapshot),
        expected_active=parent_id,
        deadline=deadline,
        cancelled=cancelled,
        repository_scope=repository_scope,
        snapshot=snapshot,
        publication_root=root,
        coordinator=coordinator,
    )
    return _generation_build_result(built, snapshot)


def _maintenance_outcome(
    status: str, reason: str, *, partial: bool, repairs: list[dict] | None = None
) -> dict:
    outcome = {
        "status": status,
        "generation_id": None,
        "sources": 0,
        "partial": partial,
        "reason": reason,
    }
    if repairs is not None:
        outcome["repairs"] = repairs
    return outcome


def _require_positive_time_budget(time_budget_seconds: object) -> None:
    if isinstance(time_budget_seconds, bool):
        raise ValueError("time_budget_seconds must be positive and finite")
    if not isinstance(time_budget_seconds, (int, float)):
        raise ValueError("time_budget_seconds must be positive and finite")
    if not math.isfinite(time_budget_seconds) or time_budget_seconds <= 0:
        raise ValueError("time_budget_seconds must be positive and finite")


def _unusable_filesystem_outcome(state_path: Path, deadline: float) -> dict | None:
    filesystem = _filesystem_check(state_path, deadline)
    if filesystem["status"] != "error":
        return None
    if filesystem["details"].get("budget_exhausted"):
        return _maintenance_outcome("deferred", "time_limit", partial=True)
    return _maintenance_outcome("error", "unsupported_filesystem", partial=False)


# Which bound a bounded refusal actually hit. Collapsing all of them into
# "source_limit" told an operator to shrink a corpus that was not the problem.
_BOUNDED_REASONS = (
    ("incremental manifest", "manifest_byte_ceiling"),
    ("total byte limit", "corpus_byte_limit"),
    ("entry limit", "corpus_entry_limit"),
    ("directory limit", "corpus_directory_limit"),
    ("depth limit", "corpus_depth_limit"),
)


def _bounded_reason(message: str) -> str:
    """The name of the bound this refusal names, or the generic source limit."""
    lowered = message.casefold()
    for fragment, reason in _BOUNDED_REASONS:
        if fragment in lowered:
            return reason
    return "source_limit"


def _value_error_outcome(exc: ValueError, repaired: list[dict]) -> dict:
    message = str(exc)
    lowered = message.casefold()
    if "limit" in lowered or "ceiling" in lowered:
        return _maintenance_outcome(
            "deferred", _bounded_reason(message), partial=True, repairs=repaired
        )
    return _maintenance_outcome(
        "error", type(exc).__name__, partial=False, repairs=repaired
    )


def _refreshed_generation(
    root_path: Path,
    state_path: Path,
    coordinator: Any,
    lease: dict[str, object],
    deadline: float,
    max_sources: int,
    force_rebuild: bool,
    repaired: list[dict],
) -> dict:
    with _MaintenanceHeartbeat(coordinator, lease, deadline=deadline) as guard:
        guard.run(
            _repair_generation_catalog,
            root_path,
            state_path,
            deadline=deadline,
            cancelled=guard.cancelled,
            repaired=repaired,
        )
        result = guard.run(
            _build_or_refresh_generation,
            root_path,
            state_path,
            deadline=deadline,
            cancelled=guard.cancelled,
            max_sources=max_sources,
            force_rebuild=force_rebuild,
            coordinator=coordinator,
        )
        result["repairs"] = repaired
        return result


def _rebuild_after_corpus_change(
    root_path: Path,
    state_path: Path,
    coordinator: object,
    lease: object,
    deadline: float,
    max_sources: int,
    repaired: list[dict],
) -> dict:
    """Capture the vault again, once. A second change defers to the next pass."""
    try:
        return _refreshed_generation(
            root_path,
            state_path,
            coordinator,
            lease,
            deadline,
            max_sources,
            True,
            repaired,
        )
    except _corpus_changed_error():
        return _maintenance_outcome(
            "deferred", "corpus_changed", partial=True, repairs=repaired
        )
    except TimeoutError:
        return _maintenance_outcome(
            "deferred", "time_limit", partial=True, repairs=repaired
        )
    except RuntimeError as exc:
        # This runs inside the caller's `except`, so the caller's own handlers
        # can no longer see what happens here. The fence can be lost during the
        # recapture just as easily as during the first pass.
        if str(exc) != "maintenance_owner_fence_lost":
            raise
        return _maintenance_outcome(
            "deferred", "maintenance_owner_lost", partial=True, repairs=repaired
        )


def _corpus_changed_error() -> type[BaseException]:
    """The error a live vault raises when it was written to mid-capture."""
    from corpus_snapshot import CorpusChanged

    return CorpusChanged


def run_generation_maintenance(
    root: Path | str | None = None,
    state_root: Path | str | None = None,
    *,
    time_budget_seconds: float = DEFAULT_GENERATION_TIME_BUDGET_SECONDS,
    max_sources: int = DEFAULT_GENERATION_SOURCE_LIMIT,
    force_rebuild: bool = False,
) -> dict:
    """Run one bounded fenced generation refresh; never mutate knowledge."""
    _require_positive_time_budget(time_budget_seconds)
    _require_positive_source_limit(max_sources)
    root_path = Path(
        root or os.environ.get("LLM_WIKI_ROOT", Path(__file__).resolve().parent.parent)
    ).resolve()
    state_path = Path(
        os.path.abspath(state_root or os.environ.get("LLM_WIKI_STATE_ROOT", root_path))
    )
    deadline = time.monotonic() + float(time_budget_seconds)
    unusable = _unusable_filesystem_outcome(state_path, deadline)
    if unusable is not None:
        return unusable
    acquired = _acquire_maintenance_owner(
        root_path, state_path, datetime.now(timezone.utc)
    )
    if acquired is None:
        return _maintenance_outcome("deferred", "maintenance_owner_busy", partial=True)
    coordinator, lease = acquired
    repaired: list[dict] = []
    try:
        return _refreshed_generation(
            root_path,
            state_path,
            coordinator,
            lease,
            deadline,
            max_sources,
            force_rebuild,
            repaired,
        )
    except TimeoutError:
        return _maintenance_outcome(
            "deferred", "time_limit", partial=True, repairs=repaired
        )
    except _corpus_changed_error():
        # The vault was written to while its snapshot was being validated. That
        # is the normal state of a vault in use, so it is captured again rather
        # than deferred: deferring would leave an active vault never refreshed.
        return _rebuild_after_corpus_change(
            root_path, state_path, coordinator, lease, deadline, max_sources, repaired
        )
    except RuntimeError as exc:
        # Another maintenance owner took the fence while this pass was working.
        # Losing that race is an ordinary outcome — the nightly timer and a
        # manual run collide — and belongs in the report, not in a traceback.
        if str(exc) != "maintenance_owner_fence_lost":
            raise
        return _maintenance_outcome(
            "deferred", "maintenance_owner_lost", partial=True, repairs=repaired
        )
    except ValueError as exc:
        return _value_error_outcome(exc, repaired)
    except (OSError, PermissionError, sqlite3.Error) as exc:
        return _maintenance_outcome(
            "error", type(exc).__name__, partial=False, repairs=repaired
        )


def _repair_queue_capabilities(state_root: Path) -> int:
    path = state_root / "run" / "queue.sqlite3"
    if not path.is_file():
        return 0
    from llm_client import probe_candidate, provider_candidates

    provider_ready = any(probe_candidate(item) for item in provider_candidates())
    repaired = {"llm.compile", "llm.flush", "llm.query"} if provider_ready else set()
    if not repaired:
        return 0
    placeholders = ",".join("?" for _ in repaired)
    from memory_queue import MemoryQueue

    queue = MemoryQueue(state_root)
    with queue._connect() as database:
        changed = database.execute(
            f"UPDATE tasks SET state='ready',blocked_capability=NULL,error_code=NULL "
            f"WHERE state='blocked' AND blocked_capability IN ({placeholders})",
            sorted(repaired),
        ).rowcount
        database.commit()
    return changed


def _run_bounded_worker(
    state_root: Path,
    *,
    deadline: float = float("inf"),
    cancelled=None,
) -> int:
    from memory_queue import (
        MemoryQueue,
        _acquire_queue_owner,
        _manual_processor,
        _release_queue_owner,
        run_worker,
    )

    MemoryQueue(state_root)
    configured = Path(
        os.environ.get(
            "LLM_WIKI_STATE_ROOT",
            os.environ.get("LLM_WIKI_ROOT", Path(__file__).resolve().parent.parent),
        )
    ).resolve()
    if configured != Path(state_root).resolve():
        return 0
    remaining = max(0, min(1, int(deadline - time.monotonic() + 0.999)))
    if remaining <= 0 or bool(cancelled and cancelled()):
        return 0
    owner = _acquire_queue_owner(
        state_root, "worker", "worker_busy", ttl_seconds=MAINTENANCE_LEASE_SECONDS
    )
    try:
        summary = run_worker(
            _manual_processor,
            max_tasks=20,
            max_seconds=remaining,
            idle_seconds=0,
            cancelled=cancelled,
        )
        return summary.processed
    finally:
        _release_queue_owner(owner)


_DEFERRED_BY_ACTION = {
    "runtime": {"runtime"},
    "transactions": {"transactions"},
    "queue": {"queue"},
    "indexes": {"index", "claims"},
    "archives": {"archives"},
    "generations": {"generation"},
}


class _RepairContext(NamedTuple):
    """Everything a repair step is allowed to read or record."""

    root_path: Path
    state_path: Path
    generated_at: datetime
    deadline: float
    rebuild_generation: bool
    selected_repairs: set[str]
    repaired: list[dict]
    repair_errors: dict[str, list[str]]
    repair_deferred: set[str]


def _validated_repairs(repair_actions: set[str] | frozenset[str] | None) -> set[str]:
    selected = set(VALID_REPAIR_ACTIONS if repair_actions is None else repair_actions)
    unknown = selected - VALID_REPAIR_ACTIONS
    if unknown:
        raise ValueError(f"unknown doctor repair actions: {sorted(unknown)}")
    return selected


def _resolved_doctor_paths(
    root: Path | str | None, state_root: Path | str | None, home: Path | str | None
) -> tuple[Path, Path, Path]:
    root_path = Path(
        root or os.environ.get("LLM_WIKI_ROOT", Path(__file__).resolve().parent.parent)
    ).resolve()
    state_path = Path(
        os.path.abspath(state_root or os.environ.get("LLM_WIKI_STATE_ROOT", root_path))
    )
    home_path = Path(home).resolve() if home is not None else Path.home().resolve()
    return root_path, state_path, home_path


def _validated_doctor_deadline(
    deadline: float | None, time_budget_seconds: float
) -> float:
    if deadline is None:
        return time.monotonic() + max(0.0, time_budget_seconds)
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
        raise ValueError("deadline must be a finite monotonic timestamp")
    return float(deadline)


def _defer_all_repairs(context: _RepairContext) -> None:
    for action in context.selected_repairs:
        context.repair_deferred.update(_DEFERRED_BY_ACTION[action])


def _repair_generations_action(guard: Any, context: _RepairContext) -> None:
    guard.run(
        _repair_generation_catalog,
        context.root_path,
        context.state_path,
        deadline=context.deadline,
        cancelled=guard.cancelled,
        repaired=context.repaired,
    )
    if not context.rebuild_generation:
        return
    result = guard.run(
        _build_or_refresh_generation,
        context.root_path,
        context.state_path,
        deadline=context.deadline,
        cancelled=guard.cancelled,
        max_sources=DEFAULT_GENERATION_SOURCE_LIMIT,
        force_rebuild=True,
    )
    if result["status"] == "built":
        context.repaired.append(
            {
                "action": "rebuild_generation",
                "generation_id": result["generation_id"],
            }
        )
        return
    if result["status"] == "deferred":
        context.repair_deferred.add("generation")


def _repair_transactions_action(
    guard: Any, coordinator: Any, context: _RepairContext
) -> None:
    recovered = guard.run(
        coordinator.recover,
        writer_wait_seconds=0,
        max_transactions=MAX_OPERATIONAL_ROWS,
        deadline=context.deadline,
        cancelled=guard.cancelled,
    )
    if recovered:
        context.repaired.append(
            {"action": "recover_transactions", "count": len(recovered)}
        )


def _record_queue_migration_repair(
    migration: Any, marker_existed: bool, context: _RepairContext
) -> None:
    if migration is None:
        return
    if marker_existed and not migration.imported and not migration.quarantined:
        return
    context.repaired.append(
        {
            "action": "migrate_queue",
            "count": migration.imported + migration.quarantined,
        }
    )


def _repair_queue_action(guard: Any, context: _RepairContext) -> bool:
    """Repair the legacy queue and report whether the v2 queue is usable."""
    from memory_queue import MemoryQueue, migrate_legacy_queue

    legacy_available = guard.run(
        _repair_leases, context.state_path, context.generated_at, context.repaired
    )
    if not legacy_available:
        context.repair_deferred.add("queue")
    marker = context.state_path / "run" / "queue-migrated-v2"
    marker_existed = _safe_kind(marker, context.state_path)[0] == "regular"
    migration = None
    if legacy_available:
        migration = guard.run(
            migrate_legacy_queue,
            context.state_path,
            deadline=context.deadline,
            cancelled=guard.cancelled,
        )
    _record_queue_migration_repair(migration, marker_existed, context)
    marker_valid = _safe_kind(marker, context.state_path)[0] == "regular"
    if migration is None and not marker_valid:
        return False
    guard.run(MemoryQueue, context.state_path)
    return True


def _index_repair_failure_message(index_after: dict) -> str:
    if index_after["details"].get("freshness") == "missing":
        return "Index repair failed: index was not created"
    return "Index repair failed: rebuilt index did not validate as fresh"


def _rebuild_and_verify_index(guard: Any, context: _RepairContext) -> None:
    guard.run(
        _rebuild_index,
        context.root_path,
        context.state_path,
        deadline=context.deadline,
        cancelled=guard.cancelled,
    )
    index_after = _index_check(
        context.state_path,
        context.generated_at,
        context.deadline,
        root=context.root_path,
    )
    if index_after["status"] == "ok":
        context.repaired.append({"action": "rebuild_index"})
        return
    context.repair_errors.setdefault("index", []).append(
        _index_repair_failure_message(index_after)
    )


def _repair_index_action(guard: Any, context: _RepairContext) -> None:
    index_before = _index_check(
        context.state_path,
        context.generated_at,
        context.deadline,
        root=context.root_path,
    )
    if not index_before["details"].get("repairable") or index_before["status"] == "ok":
        return
    index_lock = context.state_path / "cache" / ".doctor-index.lock"
    lock_token = guard.run(
        _acquire_lock,
        index_lock,
        context.state_path / "cache",
        context.generated_at,
    )
    if lock_token is None:
        context.repair_deferred.add("index")
        return
    try:
        _rebuild_and_verify_index(guard, context)
    except Exception as exc:  # noqa: BLE001
        context.repair_errors.setdefault("index", []).append(
            f"Index repair failed: {type(exc).__name__}"
        )
    finally:
        guard.cleanup(
            _release_lock, index_lock, context.state_path / "cache", lock_token
        )


def _repair_archives_action(guard: Any, context: _RepairContext) -> None:
    archive_before = _archive_check(
        context.root_path, context.state_path, context.deadline
    )
    archive_root = _archive_path(context.root_path)
    if _safe_kind(archive_root, context.root_path)[0] != "directory":
        return
    if archive_before["status"] == "ok":
        return
    from archive_daily import DailyArchiver

    guard.run(
        lambda: DailyArchiver(context.root_path, context.state_path).recover(
            deadline=context.deadline,
            cancelled=guard.cancelled,
        )
    )
    context.repaired.append({"action": "recover_archives"})


def _claim_sources(root_path: Path) -> list[Path]:
    sources = [root_path / "knowledge" / "notes"]
    projects = root_path / "knowledge" / "projects"
    if _safe_kind(projects, root_path)[0] == "directory":
        sources.append(projects)
    return sources


def _repair_claims_action(guard: Any, context: _RepairContext) -> None:
    claim_before = _claim_check(
        context.root_path, context.state_path, context.deadline
    )
    if claim_before["status"] == "ok":
        return
    from claims import ClaimIndex

    claim_index = ClaimIndex(context.state_path, vault=context.root_path)
    guard.run(
        claim_index.rebuild,
        _claim_sources(context.root_path),
        deadline=context.deadline,
        cancelled=guard.cancelled,
    )
    context.repaired.append({"action": "rebuild_claim_index"})


def _repair_queue_followups(
    guard: Any, context: _RepairContext, queue_v2_ready: bool
) -> None:
    if not queue_v2_ready:
        return
    unblocked = guard.run(_repair_queue_capabilities, context.state_path)
    if unblocked:
        context.repaired.append(
            {"action": "unblock_capabilities", "count": unblocked}
        )
    processed = guard.run(
        _run_bounded_worker,
        context.state_path,
        deadline=context.deadline,
        cancelled=guard.cancelled,
    )
    if processed:
        context.repaired.append({"action": "run_bounded_worker", "count": processed})


def _repair_state_actions(
    guard: Any, coordinator: Any, context: _RepairContext
) -> bool:
    if "runtime" in context.selected_repairs:
        guard.run(_repair_runtime, context.state_path, context.repaired)
    if "generations" in context.selected_repairs:
        _repair_generations_action(guard, context)
    if "transactions" in context.selected_repairs:
        _repair_transactions_action(guard, coordinator, context)
    if "queue" not in context.selected_repairs:
        return False
    return _repair_queue_action(guard, context)


def _repair_derived_actions(
    guard: Any, context: _RepairContext, queue_v2_ready: bool
) -> None:
    if "indexes" in context.selected_repairs:
        _repair_index_action(guard, context)
    if "archives" in context.selected_repairs:
        _repair_archives_action(guard, context)
    if "indexes" in context.selected_repairs:
        _repair_claims_action(guard, context)
    if "queue" in context.selected_repairs:
        _repair_queue_followups(guard, context, queue_v2_ready)


def _release_unentered_maintenance(
    maintenance: tuple | None, guard_entered: bool, context: _RepairContext
) -> None:
    if maintenance is None or guard_entered:
        return
    try:
        _release_maintenance_owner(*maintenance)
    except Exception as exc:  # noqa: BLE001
        context.repair_errors.setdefault("runtime", []).append(
            f"Maintenance owner release failed: {type(exc).__name__}"
        )


def _run_repairs(context: _RepairContext) -> None:
    """Run every selected repair under one maintenance owner."""
    maintenance: tuple[Any, dict[str, object]] | None = None
    guard_entered = False
    try:
        maintenance = _acquire_maintenance_owner(
            context.root_path, context.state_path, context.generated_at
        )
        if maintenance is None:
            _defer_all_repairs(context)
        else:
            coordinator, lease = maintenance
            with _MaintenanceHeartbeat(
                coordinator, lease, deadline=context.deadline
            ) as guard:
                guard_entered = True
                queue_v2_ready = _repair_state_actions(guard, coordinator, context)
                _repair_derived_actions(guard, context, queue_v2_ready)
    except Exception as exc:  # noqa: BLE001
        context.repair_errors.setdefault("runtime", []).append(
            f"Repair failed: {type(exc).__name__}"
        )
    finally:
        _release_unentered_maintenance(maintenance, guard_entered, context)


def _deferrable_checks(
    root_path: Path,
    state_path: Path,
    home_path: Path,
    generated_at: datetime,
    deadline: float,
) -> tuple[tuple[str, Callable[[], dict]], ...]:
    partial = functools.partial
    return (
        (
            "generation",
            partial(_generation_check, root_path, state_path, generated_at, deadline),
        ),
        (
            "index",
            partial(_index_check, state_path, generated_at, deadline, root=root_path),
        ),
        (
            "scheduler",
            partial(_scheduler_check, root_path, state_path, generated_at, deadline),
        ),
        ("capture", partial(_capture_check, state_path, deadline)),
        ("mcp", partial(_mcp_check, root_path)),
        (
            "integrations",
            partial(_integration_check, root_path, home_path, deadline=deadline),
        ),
        ("pyright", partial(_pyright_check, root_path, state_path, deadline=deadline)),
        (
            "lsp",
            partial(_lsp_runtime_check, state_path, generated_at, deadline=deadline),
        ),
    )


def _completed_or_deferred(
    check_id: str, operation: Callable[[], dict], deadline: float
) -> dict:
    """The LSP check owns its own budget; the rest defer once time is up."""
    if check_id != "lsp" and time.monotonic() >= deadline:
        return _result(
            check_id,
            "degraded",
            "Check not completed because the doctor time budget was exhausted.",
            {"budget_exhausted": True},
        )
    return operation()


def _collect_checks(
    root_path: Path,
    state_path: Path,
    home_path: Path,
    generated_at: datetime,
    deadline: float,
) -> list[dict]:
    checks = [
        _environment_check(root_path, state_path),
        _runtime_check(state_path),
        _filesystem_check(state_path, deadline),
        _transaction_check(state_path, generated_at, deadline),
        _queue_check(state_path, generated_at, deadline),
        _archive_check(root_path, state_path, deadline),
        _claim_check(root_path, state_path, deadline),
    ]
    for check_id, operation in _deferrable_checks(
        root_path, state_path, home_path, generated_at, deadline
    ):
        checks.append(_completed_or_deferred(check_id, operation, deadline))
    return checks


def _mark_repair_deferred(check: dict) -> None:
    check["status"] = "degraded"
    check["message"] = (
        f"{check['id'].title()} repair deferred because another owner "
        "holds the repair lock."
    )
    check["details"]["repair_deferred"] = True


def _mark_repair_failed(check: dict, errors: list[str]) -> None:
    check["status"] = "error"
    check["message"] = f"{check['id'].title()} repair failed."
    check["details"]["repair_errors"] = errors


def _apply_repair_outcomes(checks: list[dict], context: _RepairContext) -> None:
    for check in checks:
        if check["id"] in context.repair_deferred:
            _mark_repair_deferred(check)
        errors = context.repair_errors.get(check["id"])
        if errors:
            _mark_repair_failed(check, errors)


def _status_counts(checks: list[dict]) -> dict[str, int]:
    return {
        status: sum(check["status"] == status for check in checks)
        for status in VALID_STATUSES
    }


def _overall_status(counts: dict[str, int]) -> str:
    if counts["error"]:
        return "error"
    if counts["degraded"]:
        return "degraded"
    return "ok"


def _run_deletion_result(run_deletion: dict) -> dict:
    message = "Runtime history must be retained."
    if run_deletion["quiescent"]:
        message = (
            "Runtime state was observed quiescent; offline action is still required."
        )
    return _result("run_deletion", "ok", message, run_deletion)


def run_doctor(
    root: Path | str | None = None,
    state_root: Path | str | None = None,
    home: Path | str | None = None,
    repair: bool = False,
    rebuild_generation: bool = False,
    repair_actions: set[str] | frozenset[str] | None = None,
    now: datetime | None = None,
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
    deadline: float | None = None,
) -> dict:
    """Return a JSON-safe local health report; mutate only with ``repair=True``."""
    root_path, state_path, home_path = _resolved_doctor_paths(root, state_root, home)
    generated_at = _as_utc(now)
    context = _RepairContext(
        root_path=root_path,
        state_path=state_path,
        generated_at=generated_at,
        deadline=_validated_doctor_deadline(deadline, time_budget_seconds),
        rebuild_generation=rebuild_generation,
        selected_repairs=_validated_repairs(repair_actions),
        repaired=[],
        repair_errors={},
        repair_deferred=set(),
    )
    if repair:
        _run_repairs(context)

    checks = _collect_checks(
        root_path, state_path, home_path, generated_at, context.deadline
    )
    _apply_repair_outcomes(checks, context)
    run_deletion = _run_deletion_check(
        state_path,
        generated_at,
        root=root_path,
        deadline=context.deadline,
        collected={check["id"]: check for check in checks},
    )
    checks.append(_run_deletion_result(run_deletion))
    counts = _status_counts(checks)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "overall_status": _overall_status(counts),
        "repaired": context.repaired,
        "checks": checks,
        "counts": counts,
        "run_deletion": run_deletion,
    }


def degraded_summary(report: dict) -> str:
    """Return a compact bounded summary containing only actionable checks."""
    if report.get("overall_status") == "ok":
        return ""
    entries = []
    for check in report.get("checks", []):
        if check.get("status") in {"degraded", "error"}:
            entries.append(
                f"{check.get('id', 'unknown')} ({check['status']}): {check.get('message', '')}"
            )
    text = "; ".join(entries)
    if len(text) <= SUMMARY_LIMIT:
        return text
    return text[: SUMMARY_LIMIT - 3].rstrip() + "..."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check local LLM-Wiki health.")
    parser.add_argument("--repair", action="store_true", help="Apply safe idempotent repairs.")
    parser.add_argument(
        "--rebuild-generation",
        action="store_true",
        help="Explicitly rebuild the immutable evidence generation under the repair fence.",
    )
    parser.add_argument("--json", action="store_true", help="Emit the structured report as JSON.")
    parser.add_argument(
        "--time-budget",
        type=float,
        default=DEFAULT_TIME_BUDGET_SECONDS,
        help=(
            "Seconds this run may spend before unfinished checks report "
            f"a budget exhaustion (default {DEFAULT_TIME_BUDGET_SECONDS:g})."
        ),
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.time_budget) or args.time_budget <= 0:
        parser.error("--time-budget must be a positive number of seconds")
    report = run_doctor(
        repair=args.repair or args.rebuild_generation,
        rebuild_generation=args.rebuild_generation,
        time_budget_seconds=args.time_budget,
    )
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
    else:
        print(f"LLM-Wiki doctor: {report['overall_status']}")
        summary = degraded_summary(report)
        if summary:
            print(summary)
        if report["repaired"]:
            print(f"Repairs applied: {len(report['repaired'])}")
    return {"ok": 0, "degraded": 1, "error": 2}[report["overall_status"]]


if __name__ == "__main__":
    raise SystemExit(main())
