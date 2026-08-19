"""Agent-readable local health checks and conservative repairs."""

from __future__ import annotations

import argparse
import errno
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
from typing import Any

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
    for path in entries:
        if path.suffix not in {".json", ".processing"}:
            details["artifact_error"] = True
            continue
        details["legacy_retained"] += 1
        _value, problem = _read_bounded_json(path, legacy, deadline=deadline)
        if problem:
            details["legacy_malformed"] += 1
    for key, relative in (
        ("results_retained", "run/queue-results"),
        ("queue_quarantined", "run/queue-quarantine"),
    ):
        entries, truncated, error = _bounded_runtime_entries(
            state_root / relative,
            state_root,
            limit=MAX_RUNTIME_ENTRIES,
            deadline=deadline,
        )
        details[key] = len(entries)
        details["artifact_truncated"] |= truncated
        details["artifact_error"] |= error
        if any(_safe_kind(path, state_root)[0] != "regular" for path in entries):
            details["artifact_error"] = True
    if details["legacy_retained"]:
        details["deletion_codes"].append("legacy_queue_retained")
    if details["legacy_malformed"]:
        details["deletion_codes"].append("legacy_queue_malformed")
    if details["results_retained"]:
        details["deletion_codes"].append("queue_result_retained")
    if details["queue_quarantined"]:
        details["deletion_codes"].append("queue_quarantine_retained")
    if details["artifact_error"] or details["artifact_truncated"]:
        details["deletion_codes"].append("queue_artifact_state_unknown")
    return details


def _queue_check(state_root: Path, now: datetime, deadline: float) -> dict:
    database_path = state_root / "run" / "queue.sqlite3"
    database_kind = _safe_kind(database_path, state_root)[0]
    if database_kind == "regular":
        return _queue_v2_check(state_root, now, deadline)
    if database_kind == "missing" and _database_sidecar_present(database_path, state_root):
        details = _queue_artifact_state(state_root, deadline)
        details.update(read_error=True, states={state: 0 for state in QUEUE_STATES})
        details["deletion_codes"].append("queue_state_unreadable")
        return _result("queue", "error", "Queue sidecars lack a database.", details)
    if database_kind != "missing":
        details = _queue_artifact_state(state_root, deadline)
        details.update(read_error=True, states={state: 0 for state in QUEUE_STATES})
        details["deletion_codes"].append("queue_state_unreadable")
        return _result("queue", "error", "Queue database is unsafe.", details)
    queue = state_root / "run" / "queue"
    pending = 0
    permanently_failed = 0
    stale_leases = 0
    ownerless_leases = 0
    unsafe_entries = 0
    oversized_entries = 0
    scanned = 0
    truncated = False
    kind, _ = _safe_kind(queue, state_root)
    if kind == "directory":
        try:
            entries = os.scandir(queue)
            with entries:
                for entry in entries:
                    if scanned >= MAX_QUEUE_FILES or time.monotonic() >= deadline:
                        truncated = True
                        break
                    scanned += 1
                    if not (entry.name.endswith(".json") or entry.name.endswith(".processing")):
                        continue
                    task, problem = _read_bounded_json(Path(entry.path), queue)
                    if problem:
                        unsafe_entries += problem == "unsafe"
                        oversized_entries += problem == "oversized"
                        truncated = truncated or problem == "oversized"
                        if problem == "invalid" and entry.name.endswith(".json"):
                            permanently_failed += 1
                        elif entry.name.endswith(".processing"):
                            try:
                                old = entry.stat(follow_symlinks=False).st_mtime
                                if now.timestamp() - old > STALE_LEASE_SECONDS:
                                    stale_leases += 1
                                    ownerless_leases += 1
                            except OSError:
                                unsafe_entries += 1
                        continue
                    if entry.name.endswith(".json"):
                        pending += 1
                        try:
                            permanently_failed += (
                                int(task.get("attempts", 0)) >= PERMANENT_FAILURE_ATTEMPTS
                            )
                        except (ValueError, TypeError):
                            permanently_failed += 1
                    else:
                        stale, owned = _lease_state(task, now)
                        stale_leases += stale
                        ownerless_leases += stale and not owned
        except OSError:
            unsafe_entries += 1
    elif kind not in {"missing"}:
        unsafe_entries += 1
    details = {
        "pending": pending,
        "permanently_failed": permanently_failed,
        "stale_leases": stale_leases,
        "ownerless_leases": ownerless_leases,
        "unsafe_entries": unsafe_entries,
        "oversized_entries": oversized_entries,
        "scanned": scanned,
        "truncated": truncated,
        "read_error": False,
    }
    artifacts = _queue_artifact_state(state_root, deadline)
    details.update(artifacts)
    if permanently_failed:
        status, message = "error", f"Queue has {permanently_failed} permanently failed task(s)."
    elif pending or stale_leases or unsafe_entries or oversized_entries or truncated:
        status, message = (
            "degraded",
            f"Queue has {pending} pending task(s) and {stale_leases} stale lease(s).",
        )
    else:
        status, message = "ok", "Queue has no pending or stale work."
    if artifacts["deletion_codes"]:
        if artifacts["artifact_error"]:
            status = "error"
        elif status == "ok":
            status = "degraded"
    return _result("queue", status, message, details)


def _read_busy_ms(deadline: float | None) -> int:
    """Wait out a brief commit lock, keeping budget left to report what happened.

    Spending the whole remaining budget on the wait would turn every busy
    database into an indistinguishable "budget exhausted" verdict.
    """
    if deadline is None:
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


def _transaction_check(state_root: Path, now: datetime, deadline: float = float("inf")) -> dict:
    path = state_root / "run" / "markdown-transactions.sqlite3"
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
    kind, _ = _safe_kind(path, state_root)
    if kind == "missing":
        artifacts, unsafe = _transaction_artifacts(state_root, deadline)
        if artifacts or unsafe or _database_sidecar_present(path, state_root):
            details["read_error"] = True
            details["deletion_codes"].append("transaction_state_unreadable")
            return _result(
                "transactions",
                "error",
                "Transaction artifacts lack readable state.",
                details,
            )
        return _result("transactions", "ok", "No transaction database exists.", details)
    if kind != "regular":
        details["read_error"] = True
        details["deletion_codes"].append("transaction_state_unreadable")
        return _result("transactions", "error", "Transaction database is unsafe.", details)
    try:
        with _readonly_database(path, state_root, deadline=deadline) as database:
            if _deadline_reached(deadline):
                raise TimeoutError("transaction check deadline")
            tables = _tables(database, deadline)
            if "transaction" not in tables:
                raise sqlite3.DatabaseError("transaction table missing")
            transaction_columns = _columns(database, "transaction", deadline)
            operation_columns = (
                _columns(database, "operation", deadline) if "operation" in tables else set()
            )
            if not TRANSACTION_REQUIRED_COLUMNS.issubset(
                transaction_columns
            ) or not OPERATION_REQUIRED_COLUMNS.issubset(operation_columns):
                details["codes"].append("transaction_metadata_missing")
                details["deletion_codes"].append("transaction_state_corrupt")
                return _result(
                    "transactions",
                    "error",
                    "Transaction metadata is incomplete.",
                    details,
                )
            transaction_query = (
                "SELECT id, operation_id, request_hash, state, preconditions_json, "
                "plan_hash, created_at, updated_at, artifacts_pruned_at "
                'FROM "transaction"'
            )
            transaction_rows = database.execute(
                transaction_query + " LIMIT ?", (MAX_OPERATIONAL_ROWS + 1,)
            ).fetchall()
            if len(transaction_rows) > MAX_OPERATIONAL_ROWS:
                details["codes"].append("transaction_scan_truncated")
                details["deletion_codes"].append("transaction_state_unknown")
                transaction_rows = transaction_rows[:MAX_OPERATIONAL_ROWS]
            codes: set[str] = set()
            operation_rows = database.execute(
                "SELECT transaction_id, position, kind, path, before_hash, "
                'after_hash, parent_device, parent_inode, applied FROM "operation" '
                "LIMIT ?",
                (MAX_OPERATIONAL_ROWS + 1,),
            ).fetchall()
            corrupt = False
            if len(operation_rows) > MAX_OPERATIONAL_ROWS:
                details["codes"].append("transaction_operation_scan_truncated")
                details["deletion_codes"].append("transaction_state_unknown")
                operation_rows = operation_rows[:MAX_OPERATIONAL_ROWS]
            known_ids = {row["id"] for row in transaction_rows if isinstance(row["id"], str)}
            operation_positions: dict[str, list[int]] = {
                transaction_id: [] for transaction_id in known_ids
            }
            digest = re.compile(r"[0-9a-f]{64}")
            for operation in operation_rows:
                transaction_id = operation["transaction_id"]
                position = operation["position"]
                before_hash = operation["before_hash"]
                after_hash = operation["after_hash"]
                valid_hashes = all(
                    value == "absent"
                    or isinstance(value, str)
                    and digest.fullmatch(value) is not None
                    for value in (before_hash, after_hash)
                )
                kind = operation["kind"]
                valid_transition = (
                    kind == "create"
                    and before_hash == "absent"
                    and after_hash != "absent"
                    or kind == "replace"
                    and before_hash != "absent"
                    and after_hash != "absent"
                    or kind == "delete"
                    and before_hash != "absent"
                    and after_hash == "absent"
                )
                valid_shape = (
                    isinstance(transaction_id, str)
                    and transaction_id in known_ids
                    and isinstance(position, int)
                    and not isinstance(position, bool)
                    and position >= 0
                    and kind in {"create", "replace", "delete"}
                    and isinstance(operation["path"], str)
                    and bool(operation["path"])
                    and valid_hashes
                    and valid_transition
                    and isinstance(operation["parent_device"], int)
                    and isinstance(operation["parent_inode"], int)
                    and operation["applied"] in {0, 1}
                )
                if not valid_shape:
                    corrupt = True
                    continue
                operation_positions[transaction_id].append(position)
            cutoff = now - timedelta(days=UNDO_RETENTION_DAYS)
            for row in transaction_rows:
                if _deadline_reached(deadline):
                    raise TimeoutError("transaction check deadline")
                state = row["state"]
                transaction_id = row["id"]
                if not isinstance(state, str) or state not in TRANSACTION_STATES:
                    details["deletion_codes"].append("transaction_state_unknown")
                    continue
                states[state] += 1
                if state in {"conflicted", "quarantined"}:
                    code_row = (
                        database.execute(
                            'SELECT error_code FROM "transaction" WHERE id=?', (row["id"],)
                        ).fetchone()
                        if "error_code" in transaction_columns
                        else None
                    )
                    if code_row is not None and code_row[0]:
                        codes.add(str(code_row[0]))
                created = _parse_utc(row["created_at"])
                updated = _parse_utc(row["updated_at"])
                pruned = (
                    _parse_utc(row["artifacts_pruned_at"])
                    if row["artifacts_pruned_at"] is not None
                    else None
                )
                try:
                    preconditions = json.loads(row["preconditions_json"])
                except (TypeError, ValueError):
                    preconditions = None
                valid_plan_hash = (state == "preparing" and row["plan_hash"] == "") or (
                    isinstance(row["plan_hash"], str)
                    and digest.fullmatch(row["plan_hash"]) is not None
                )
                row_corrupt = not (
                    isinstance(transaction_id, str)
                    and re.fullmatch(r"[0-9a-z_-]{1,128}", transaction_id)
                    and isinstance(row["operation_id"], str)
                    and bool(row["operation_id"])
                    and isinstance(row["request_hash"], str)
                    and digest.fullmatch(row["request_hash"])
                    and isinstance(preconditions, dict)
                    and valid_plan_hash
                    and created is not None
                    and updated is not None
                    and created <= updated
                    and (row["artifacts_pruned_at"] is None or pruned is not None)
                )
                positions = operation_positions.get(transaction_id, [])
                if positions != list(range(len(positions))):
                    row_corrupt = True
                if state not in {"preparing", "discarded"} and not positions:
                    row_corrupt = True
                corrupt = corrupt or row_corrupt
                artifact = state_root / "run" / "transactions" / transaction_id
                if (
                    state == "committed"
                    and updated is not None
                    and updated >= cutoff
                    and row["artifacts_pruned_at"] is None
                    and re.fullmatch(r"[0-9a-z_-]{1,128}", transaction_id) is not None
                    and _safe_kind(artifact, state_root)[0] == "directory"
                ):
                    details["undo_retained"] += 1
            details["codes"] = sorted(set(details["codes"]) | codes)
            if "project_leases" in tables:
                rows = database.execute(
                    "SELECT expires_at FROM project_leases LIMIT ?",
                    (MAX_OPERATIONAL_ROWS + 1,),
                ).fetchall()
                if len(rows) > MAX_OPERATIONAL_ROWS:
                    details["deletion_codes"].append("project_lease_state_unknown")
                details["live_project_leases"] = sum(
                    (_parse_utc(row[0]) or datetime.max.replace(tzinfo=timezone.utc)) > now
                    for row in rows[:MAX_OPERATIONAL_ROWS]
                )
            if "writer_owners" in tables:
                rows = database.execute(
                    "SELECT * FROM writer_owners LIMIT ?", (MAX_OPERATIONAL_ROWS + 1,)
                ).fetchall()
                if len(rows) > MAX_OPERATIONAL_ROWS:
                    details["deletion_codes"].append("writer_state_unknown")
                if any(
                    not _owner_row_known(row, pid_column="process_id")
                    for row in rows[:MAX_OPERATIONAL_ROWS]
                ):
                    details["deletion_codes"].append("writer_state_unknown")
                details["live_writers"] = sum(
                    _live_owner(row, now, pid_column="process_id")
                    for row in rows[:MAX_OPERATIONAL_ROWS]
                )
            if "maintenance_owners" in tables:
                rows = database.execute(
                    "SELECT * FROM maintenance_owners LIMIT ?",
                    (MAX_OPERATIONAL_ROWS + 1,),
                ).fetchall()
                if len(rows) > MAX_OPERATIONAL_ROWS:
                    details["deletion_codes"].append("maintenance_state_unknown")
                if any(
                    (row["owner_token"] if "owner_token" in row.keys() else True)
                    and not _owner_row_known(row, pid_column="process_id")
                    for row in rows[:MAX_OPERATIONAL_ROWS]
                ):
                    details["deletion_codes"].append("maintenance_state_unknown")
                details["live_maintenance_owners"] = sum(
                    _live_owner(row, now, pid_column="process_id")
                    for row in rows[:MAX_OPERATIONAL_ROWS]
                )
            artifacts, unsafe_artifacts = _transaction_artifacts(state_root, deadline)
            if unsafe_artifacts or artifacts - known_ids:
                details["deletion_codes"].append("transaction_artifact_state_unknown")
            for row in transaction_rows:
                transaction_id = row["id"]
                if not isinstance(transaction_id, str) or transaction_id not in known_ids:
                    corrupt = True
                    continue
                artifact_present = transaction_id in artifacts
                expects_artifacts = (
                    row["state"] != "discarded" and row["artifacts_pruned_at"] is None
                )
                if artifact_present != expects_artifacts:
                    corrupt = True
            if corrupt:
                details["codes"].append("transaction_metadata_corrupt")
                details["deletion_codes"].append("transaction_state_corrupt")
    except (OSError, sqlite3.Error, TimeoutError, ValueError):
        details["read_error"] = True
        details["deletion_codes"].append("transaction_state_unreadable")
        return _result("transactions", "error", "Transaction state is unreadable.", details)
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
    status = (
        "error"
        if states["conflicted"] or states["quarantined"] or invalid_state
        else "degraded"
        if problem
        else "ok"
    )
    if any(states.get(state, 0) for state in ("preparing", "prepared", "applying")):
        details["deletion_codes"].append("transaction_nonterminal")
    if states["conflicted"]:
        details["deletion_codes"].append("transaction_conflicted")
    if states["quarantined"]:
        details["deletion_codes"].append("transaction_quarantined")
    if details["undo_retained"]:
        details["deletion_codes"].append("transaction_undo_retained")
    if details["live_project_leases"]:
        details["deletion_codes"].append("project_lease_live")
    if details["live_writers"]:
        details["deletion_codes"].append("writer_live")
    if details["live_maintenance_owners"]:
        details["deletion_codes"].append("maintenance_owner_live")
    return _result(
        "transactions",
        status,
        "Transaction state requires operator attention."
        if problem or invalid_state
        else "Transaction state is healthy.",
        details,
    )


def _queue_v2_check(state_root: Path, now: datetime, deadline: float) -> dict:
    path = state_root / "run" / "queue.sqlite3"
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
    details.update(_queue_artifact_state(state_root, deadline))
    marker = state_root / "run" / "queue-migrated-v2"
    marker_kind = _safe_kind(marker, state_root)[0]
    details["migration"] = "complete" if marker_kind == "regular" else "pending"
    if marker_kind not in {"missing", "regular"}:
        details["read_error"] = True
        details["deletion_codes"].append("queue_migration_state_unknown")
    if marker_kind == "regular" and details["legacy_retained"]:
        details["migration"] = "conflict"
    try:
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
                return _result(
                    "queue",
                    "error",
                    "Queue task metadata is incomplete.",
                    details,
                )
            rows = database.execute(
                "SELECT * FROM tasks LIMIT ?", (MAX_OPERATIONAL_ROWS + 1,)
            ).fetchall()
            if len(rows) > MAX_OPERATIONAL_ROWS:
                details["codes"].append("queue_scan_truncated")
                details["deletion_codes"].append("queue_state_unknown")
                rows = rows[:MAX_OPERATIONAL_ROWS]
            codes: set[str] = set()
            capabilities: set[str] = set()
            references: set[str] = set()
            result_hashes: dict[str, object] = {}
            unknown_state = False
            corrupt_metadata = False
            for row in rows:
                if _deadline_reached(deadline):
                    raise TimeoutError("queue check deadline")
                row_columns = set(row.keys())
                state = row["state"]
                if not isinstance(state, str) or state not in QUEUE_STATES:
                    unknown_state = True
                    continue
                states[state] += 1
                if not {"error_code", "blocked_capability"}.issubset(row_columns):
                    corrupt_metadata = True
                    continue
                error_code = row["error_code"]
                blocked_capability = row["blocked_capability"]
                valid_error_code = error_code is None or (
                    isinstance(error_code, str)
                    and 1 <= len(error_code) <= 200
                    and not any(char in error_code for char in "\r\n")
                )
                metadata_matches_state = (
                    state == "ready"
                    and valid_error_code
                    and blocked_capability is None
                    or state in {"leased", "succeeded"}
                    and error_code is None
                    and blocked_capability is None
                    or state == "blocked"
                    and error_code is not None
                    and valid_error_code
                    and isinstance(blocked_capability, str)
                    and bool(blocked_capability)
                    or state == "dead"
                    and error_code is not None
                    and valid_error_code
                    and blocked_capability is None
                    or state == "cancelled"
                    and error_code == "cancelled"
                    and blocked_capability is None
                )
                if not metadata_matches_state:
                    corrupt_metadata = True
                if error_code:
                    codes.add(str(error_code))
                if blocked_capability:
                    capabilities.add(str(blocked_capability))
                if "result_reference" in row_columns and row["result_reference"]:
                    reference = str(row["result_reference"])
                    references.add(reference)
                    result_hashes[reference] = (
                        row["result_sha256"] if "result_sha256" in row_columns else None
                    )
                if (
                    state == "leased"
                    and "lease_expires_at" in row_columns
                    and (
                        _parse_utc(row["lease_expires_at"])
                        or datetime.min.replace(tzinfo=timezone.utc)
                    )
                    > now
                ):
                    details["live_workers"] += 1
            details["codes"] = sorted(set(details["codes"]) | codes)
            details["capabilities"] = sorted(capabilities)
            if unknown_state:
                details["deletion_codes"].append("queue_state_unknown")
            if corrupt_metadata:
                details["deletion_codes"].append("queue_state_corrupt")
            if rows:
                details["deletion_codes"].append("queue_task_retained")
            if "source_failures" in tables:
                source_failures = database.execute(
                    "SELECT 1 FROM source_failures LIMIT ?",
                    (MAX_OPERATIONAL_ROWS + 1,),
                ).fetchall()
                details["source_failures"] = len(source_failures)
                if source_failures:
                    details["deletion_codes"].append("queue_source_failure_retained")
                if len(source_failures) > MAX_OPERATIONAL_ROWS:
                    details["deletion_codes"].append("queue_source_failure_state_unknown")
            if "source_fences" in tables:
                source_fences = database.execute(
                    "SELECT 1 FROM source_fences LIMIT ?",
                    (MAX_OPERATIONAL_ROWS + 1,),
                ).fetchall()
                details["source_fences"] = len(source_fences)
                if source_fences:
                    details["deletion_codes"].append("queue_source_fence_retained")
                if len(source_fences) > MAX_OPERATIONAL_ROWS:
                    details["deletion_codes"].append("queue_source_fence_state_unknown")
            if "queue_ownership" in tables:
                owner_rows = database.execute(
                    "SELECT * FROM queue_ownership LIMIT ?",
                    (MAX_OPERATIONAL_ROWS + 1,),
                ).fetchall()
                if len(owner_rows) > MAX_OPERATIONAL_ROWS:
                    details["deletion_codes"].append("queue_owner_state_unknown")
                for row in owner_rows[:MAX_OPERATIONAL_ROWS]:
                    if row["token"] is None:
                        continue
                    if not _owner_row_known(row, pid_column="pid"):
                        details["deletion_codes"].append("queue_owner_state_unknown")
                        continue
                    if not _live_owner(row, now, pid_column="pid"):
                        continue
                    if row["role"] == "worker":
                        details["live_workers"] += 1
                    if row["role"] == "migration":
                        details["live_migrations"] += 1
            results = state_root / "run" / "queue-results"
            for reference in references:
                try:
                    reference_path = Path(reference)
                    if reference_path.is_absolute() or ".." in reference_path.parts:
                        raise PermissionError("unsafe queue result reference")
                    candidate = state_root / reference_path
                    if candidate.parent.resolve(strict=True) != results.resolve(strict=True):
                        raise PermissionError("queue result reference escapes result root")
                    raw = read_runtime_bytes(
                        candidate,
                        state_root,
                        max_bytes=MAX_QUEUE_RESULT_BYTES,
                        owner_only=True,
                    )
                except (OSError, PermissionError, ValueError):
                    details["results_invalid"] += 1
                    continue
                expected = result_hashes.get(reference)
                if not isinstance(expected, str) or hashlib.sha256(raw).hexdigest() != expected:
                    details["results_invalid"] += 1
    except (OSError, PermissionError, sqlite3.Error, TimeoutError, ValueError):
        details["read_error"] = True
        details["deletion_codes"].append("queue_state_unreadable")
        return _result("queue", "error", "Queue state is unreadable.", details)
    if time.monotonic() >= deadline:
        details["budget_exhausted"] = True
    if (
        unknown_state
        or corrupt_metadata
        or details["results_invalid"]
        or details["migration"] == "conflict"
    ):
        status = "error"
    elif (
        states["ready"]
        or states["leased"]
        or states["blocked"]
        or details["migration"] == "pending"
    ):
        status = "degraded"
    else:
        status = "ok"
    if details["live_workers"]:
        details["deletion_codes"].append("queue_worker_live")
    if details["live_migrations"]:
        details["deletion_codes"].append("queue_migration_live")
    if details["results_invalid"]:
        details["deletion_codes"].append("queue_result_state_unknown")
    return _result(
        "queue",
        status,
        "Queue state requires operator attention." if status != "ok" else "Queue state is healthy.",
        details,
    )


def _archive_check(root: Path, state_root: Path, deadline: float = float("inf")) -> dict:
    archive = _archive_path(root)
    quarantine = state_root / "run" / "archive-quarantine"
    details = {
        "bags": 0,
        "duplicates": 0,
        "quarantined": 0,
        "index": "missing",
        "codes": [],
        "read_error": False,
        "deletion_codes": [],
    }
    quarantine_entries, truncated, error = _bounded_runtime_entries(
        quarantine,
        state_root,
        limit=MAX_RUNTIME_ENTRIES,
        deadline=deadline,
    )
    details["quarantined"] = len(quarantine_entries)
    if details["quarantined"]:
        details["deletion_codes"].append("archive_quarantine_retained")
    if (
        truncated
        or error
        or any(_safe_kind(item, state_root)[0] != "regular" for item in quarantine_entries)
    ):
        details["read_error"] = True
        details["deletion_codes"].append("archive_quarantine_state_unknown")
    archive_kind = _safe_kind(archive, root)
    if archive_kind[0] == "missing":
        return _result("archives", "ok", "No archive exists.", details)
    if archive_kind[0] != "directory":
        details["read_error"] = True
        details["deletion_codes"].append("archive_state_unreadable")
        return _result("archives", "error", "Archive root is unsafe.", details)
    try:
        months, month_truncated, month_error = _bounded_runtime_entries(
            archive, root, limit=121, deadline=deadline
        )
        if month_error:
            raise OSError("archive month scan failed")
        months = [
            month
            for month in months
            if _safe_kind(month, root)[0] == "directory"
            and re.fullmatch(r"\d{4}-\d{2}", month.name)
        ]
        if month_truncated or len(months) > 120:
            details["codes"].append("archive_scan_truncated")
            details["deletion_codes"].append("archive_state_unknown")
            months = months[:120]
        bags = []
        for month in months:
            entries, entry_truncated, entry_error = _bounded_runtime_entries(
                month,
                root,
                limit=MAX_RUNTIME_ENTRIES + 1,
                deadline=deadline,
            )
            if entry_error:
                raise OSError("archive bag scan failed")
            if entry_truncated:
                details["codes"].append("archive_scan_truncated")
                details["deletion_codes"].append("archive_state_unknown")
            for item in entries:
                if _safe_kind(item, root)[0] == "directory" and item.name.startswith("bag-"):
                    bags.append(item)
                    if len(bags) > MAX_RUNTIME_ENTRIES:
                        details["codes"].append("archive_scan_truncated")
                        details["deletion_codes"].append("archive_state_unknown")
                        bags = bags[:MAX_RUNTIME_ENTRIES]
                        break
            if len(bags) >= MAX_RUNTIME_ENTRIES:
                break
        details["bags"] = len(bags)
        seen: set[tuple[object, object]] = set()
        bag_paths: set[str] = set()
        for bag in bags:
            if _deadline_reached(deadline):
                raise TimeoutError("archive check deadline")
            manifest, problem = _read_bounded_json(
                bag / "archive-manifest.json", archive, max_bytes=MAX_MANIFEST_BYTES
            )
            if problem or not isinstance(manifest, dict):
                details["codes"].append("archive_manifest_invalid")
                continue
            key = (manifest.get("logical_daily_id"), manifest.get("source_hash"))
            if key in seen:
                details["duplicates"] += 1
            seen.add(key)
            bag_paths.add(bag.relative_to(root).as_posix())
        index_kind = _safe_kind(archive / "archive-index.json", root)[0]
        index, problem = (
            _read_bounded_json(
                archive / "archive-index.json",
                archive,
                max_bytes=MAX_MANIFEST_BYTES,
                deadline=deadline,
            )
            if index_kind == "regular"
            else (None, "missing" if index_kind == "missing" else "unsafe")
        )
        if not problem and isinstance(index, dict):
            indexed = index.get("bags", [])
            indexed_paths = {
                str(item.get("bag_path"))
                for item in indexed
                if isinstance(item, dict) and isinstance(item.get("bag_path"), str)
            }
            details["index"] = (
                "valid"
                if index.get("schema_version") == "archive-index/v1"
                and indexed_paths == bag_paths
                and len(indexed_paths) == len(indexed)
                else "invalid"
            )
        else:
            details["index"] = "invalid" if problem != "missing" else "missing"
    except (OSError, TimeoutError):
        details["codes"].append("archive_unreadable")
        details["read_error"] = True
        details["deletion_codes"].append("archive_state_unreadable")
    problem = (
        details["duplicates"]
        or details["quarantined"]
        or details["codes"]
        or details["index"] == "invalid"
    )
    return _result(
        "archives",
        "error" if details["codes"] else "degraded" if problem else "ok",
        "Archive state requires operator attention." if problem else "Archive state is healthy.",
        details,
    )


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
            compatible = ClaimIndex._schema_compatible(database)
            details["index"] = "valid" if compatible else "invalid"
            if compatible:
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
    except (OSError, PermissionError, sqlite3.Error, TimeoutError, ValueError):
        details["index"] = "invalid"
        details["read_error"] = True
    status = (
        "error" if details["index"] == "invalid" else "degraded" if details["diagnostics"] else "ok"
    )
    return _result(
        "claims",
        status,
        "Claim index requires operator attention." if status != "ok" else "Claim index is healthy.",
        details,
    )


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
    from operational_ownership import (
        OperationalOwnershipError,
        OwnershipRegistry,
    )

    def result(codes: list[str]) -> dict[str, object]:
        blockers = [{"code": code} for code in sorted(set(codes))]
        return {
            "schema_version": "run-deletion-snapshot/v1",
            "quiescent": not blockers,
            "permit": False,
            "offline_action_required": True,
            "blockers": blockers,
        }

    state_path = Path(state_root)
    root_path = Path(root) if root is not None else state_path
    try:
        require_reliability_v3_adopted(root=root_path, state_root=state_path)
    except ReliabilityV3ValidationError as exc:
        code = (
            "legacy_protocol_unquiesced" if exc.code == "legacy_protocol_unquiesced" else exc.code
        )
        return result([code])

    snapshot_deadline = min(deadline, time.monotonic() + 20.0)
    if _deadline_reached(snapshot_deadline):
        return result(["run_deletion_state_unknown"])
    registry = OwnershipRegistry._from_adopted_database(
        state_path,
        state_path / "run" / "markdown-transactions-v3.sqlite3",
    )
    try:
        owner = registry.acquire("runtime-deletion-check", scope="global")
    except (OperationalOwnershipError, OSError, sqlite3.Error, ValueError) as exc:
        code = getattr(exc, "code", "runtime_deletion_check_unavailable")
        return result([str(code)])

    codes: list[str] = []
    try:
        try:
            codes.extend(
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
                details = check.get("details", {})
                codes.extend(str(code) for code in details.get("deletion_codes", []))
                if details.get("read_error") and not details.get("deletion_codes"):
                    codes.append(f"{check['id']}_state_unreadable")
        except (OSError, PermissionError, sqlite3.Error, TimeoutError, ValueError):
            codes.append("run_deletion_state_unknown")
        if _deadline_reached(snapshot_deadline):
            codes.append("run_deletion_state_unknown")
    finally:
        try:
            registry.release(owner)
        except (OperationalOwnershipError, OSError, sqlite3.Error, ValueError):
            codes.append("runtime_deletion_check_release_failed")
    return result(codes)


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


def _valid_lsp_lease(record: dict[str, Any], owner_nonce: str) -> bool:
    heartbeat = _parse_lsp_timestamp(record.get("heartbeat_at"))
    expires = _parse_lsp_timestamp(record.get("expires_at"))
    return (
        set(record) == _LSP_LEASE_FIELDS
        and isinstance(record.get("schema_version"), int)
        and not isinstance(record.get("schema_version"), bool)
        and record.get("schema_version") == 1
        and record.get("owner_nonce") == owner_nonce
        and isinstance(record.get("generation_nonce"), str)
        and _LSP_OWNER_NONCE.fullmatch(record["generation_nonce"]) is not None
        and _lsp_positive_pid(record.get("manager_pid"))
        and _lsp_positive_pid(record.get("server_pid"))
        and record.get("state") == "live"
        and heartbeat is not None
        and expires is not None
        and heartbeat < expires
    )


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
    snapshots: list[_LspOwnerSnapshot] = []
    unreadable = False
    try:
        try:
            owner_names, truncated = _list_posix_lsp_names(
                lsp_fd,
                observed_limit=MAX_LSP_OWNER_ROWS + 1,
                deadline=deadline,
            )
        except (OSError, ValueError, TimeoutError):
            return [], True, False
        unreadable |= truncated
        for owner_name in sorted(owner_names[:MAX_LSP_OWNER_ROWS]):
            if _LSP_OWNER_NONCE.fullmatch(owner_name) is None:
                unreadable = True
                continue
            owner_fd: int | None = None
            present: frozenset[str] = frozenset()
            records: dict[str, dict[str, Any] | None] = {name: None for name in _LSP_RECORD_NAMES}
            try:
                owner_fd = _open_posix_lsp_directory(lsp_fd, owner_name, deadline)
                child_names, child_truncated = _list_posix_lsp_names(
                    owner_fd,
                    observed_limit=len(_LSP_OWNER_ENTRY_NAMES) + 1,
                    deadline=deadline,
                )
                unreadable |= child_truncated
                bounded_names = child_names[: len(_LSP_OWNER_ENTRY_NAMES)]
                present = frozenset(bounded_names)
                if "cancellation" not in present:
                    unreadable = True
                for child_name in bounded_names:
                    if child_name not in _LSP_OWNER_ENTRY_NAMES:
                        unreadable = True
                        continue
                    if child_name == "cancellation":
                        cancellation_fd = _open_posix_lsp_directory(owner_fd, child_name, deadline)
                        os.close(cancellation_fd)
                        continue
                    try:
                        records[child_name] = _read_posix_lsp_record(owner_fd, child_name, deadline)
                    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                        unreadable = True
            except TimeoutError:
                unreadable = True
                snapshots.append(
                    (
                        owner_name,
                        present,
                        records["owner.json"],
                        records["lease.json"],
                        records["failure.json"],
                    )
                )
                break
            except (OSError, ValueError):
                unreadable = True
            finally:
                if owner_fd is not None:
                    os.close(owner_fd)
            snapshots.append(
                (
                    owner_name,
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


def _lsp_runtime_check(
    state_root: Path,
    now: datetime,
    *,
    deadline: float = float("inf"),
) -> dict:
    """Bound the live and retained LSP owner evidence under run/lsp."""
    codes: list[str] = []
    owners: list[dict[str, Any]] = []
    snapshots, unreadable, absent = _snapshot_lsp_runtime(state_root, deadline)
    if _deadline_reached(deadline):
        unreadable = True
        absent = False
    if absent:
        return _result(
            "lsp",
            "ok",
            "No LSP runtime owners are present.",
            {
                "codes": codes,
                "owners": owners,
                "deletion_codes": [],
                "read_error": False,
            },
        )
    for entry_name, child_names, owner, lease, failure in snapshots:
        if _deadline_reached(deadline):
            unreadable = True
            break
        semantic_codes: list[str] = []
        owner_record = {
            "owner_nonce": entry_name,
            "live": False,
            "failure_evidence": False,
            "failure_age_days": None,
        }
        if (
            "owner.json" not in child_names
            or owner is None
            or "lease.json" in child_names
            and lease is None
        ):
            unreadable = True
        if owner is not None and not _valid_lsp_owner(owner, entry_name):
            unreadable = True
            owner = None
        if owner is not None:
            owner_started_at = _parse_lsp_timestamp(owner.get("started_at"))
            if owner_started_at is not None and owner_started_at > now:
                unreadable = True
        if lease is not None and not _valid_lsp_lease(lease, entry_name):
            unreadable = True
            lease = None
        live = False
        heartbeat_at: datetime | None = None
        if isinstance(owner, dict) and isinstance(lease, dict):
            owner_pid = owner.get("owner_pid")
            manager_pid = lease.get("manager_pid")
            server_pid = lease.get("server_pid")
            expires_at = _parse_lsp_timestamp(lease.get("expires_at"))
            heartbeat_at = _parse_lsp_timestamp(lease.get("heartbeat_at"))
            started_at = _parse_lsp_timestamp(owner.get("started_at"))
            matching = (
                owner.get("owner_nonce") == entry_name
                and lease.get("owner_nonce") == entry_name
                and owner.get("generation_nonce") == lease.get("generation_nonce")
                and owner_pid == server_pid
                and started_at is not None
                and heartbeat_at is not None
                and started_at <= heartbeat_at <= now
            )
            if not matching:
                unreadable = True
            pids = (manager_pid, server_pid)
            if (
                matching
                and expires_at is not None
                and expires_at > now
                and all(
                    isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 for pid in pids
                )
            ):
                pid_states: list[str] = []
                try:
                    for pid in pids:
                        _require_lsp_deadline(deadline)
                        try:
                            pid_state = _lsp_pid_state(pid)
                        finally:
                            _require_lsp_deadline(deadline)
                        pid_states.append(pid_state)
                    if "unknown" in pid_states:
                        unreadable = True
                    live = all(state == "alive" for state in pid_states)
                except TimeoutError:
                    unreadable = True
                    if _deadline_reached(deadline):
                        break
                except Exception:  # noqa: BLE001
                    unreadable = True
        owner_record["live"] = live
        if live:
            semantic_codes.append("lsp_owner_live")
        if "failure.json" in child_names:
            if failure is None:
                unreadable = True
            elif not _valid_lsp_failure(failure, entry_name):
                unreadable = True
                failure = None
            if isinstance(owner, dict) and isinstance(failure, dict):
                owner_started_at = _parse_lsp_timestamp(owner.get("started_at"))
                failed_at = _parse_lsp_timestamp(failure.get("timestamp"))
                if (
                    failure.get("generation_nonce") != owner.get("generation_nonce")
                    or (
                        "server_pid" in failure
                        and failure.get("server_pid") != owner.get("owner_pid")
                    )
                    or owner_started_at is None
                    or failed_at is None
                    or not owner_started_at <= failed_at <= now
                ):
                    unreadable = True
            age_days: float | None = None
            if isinstance(failure, dict):
                failed_at = _parse_lsp_timestamp(failure.get("timestamp"))
                if failed_at is not None:
                    age_days = (now - failed_at).total_seconds() / 86400.0
            owner_record["failure_evidence"] = True
            owner_record["failure_age_days"] = age_days
            if age_days is None or age_days < LSP_FAILURE_RETENTION.total_seconds() / 86400.0:
                semantic_codes.append("lsp_failure_evidence_retained")
        elif not live:
            crash_at = heartbeat_at
            if crash_at is None and isinstance(owner, dict):
                crash_at = _parse_lsp_timestamp(owner.get("started_at"))
            if crash_at is not None:
                owner_record["failure_evidence"] = True
                owner_record["failure_age_days"] = (now - crash_at).total_seconds() / 86400.0
            crash_age = owner_record["failure_age_days"]
            if crash_age is None or crash_age < 7:
                semantic_codes.append("lsp_failure_evidence_retained")
        if _deadline_reached(deadline):
            unreadable = True
            break
        owners.append(owner_record)
        codes.extend(semantic_codes)
    if unreadable and "lsp_state_unreadable" not in codes:
        codes.append("lsp_state_unreadable")
    status = "ok" if not codes else "degraded"
    return _result(
        "lsp",
        status,
        "LSP runtime owners are bounded."
        if status == "ok"
        else "LSP runtime owners are live or retained.",
        {
            "codes": codes,
            "owners": owners,
            "deletion_codes": list(codes),
            "read_error": unreadable,
        },
    )


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
    if isinstance(max_sources, bool) or not isinstance(max_sources, int) or max_sources < 1:
        raise ValueError("max_sources must be a positive integer")
    catalog_path = state_root / "cache" / "evidence-graph" / "catalog.sqlite3"
    invalid_details: dict[str, object] = {"catalog": "invalid", "repairable": True}
    diagnostic_manifest: dict[str, object] | None = None
    generation_path: Path | None = None
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
    try:
        with _readonly_database(catalog_path, state_root, deadline=deadline) as database:
            database.set_progress_handler(lambda: int(_deadline_reached(deadline)), 1000)
            integrity = database.execute("PRAGMA integrity_check(1)").fetchone()
            tables = _tables(database, deadline)
            required = {"generations", "catalog_state", "activation_history"}
            if integrity is None or integrity[0] != "ok" or not required.issubset(tables):
                raise sqlite3.DatabaseError("catalog integrity or schema failed")
            journal = str(database.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
            synchronous = database.execute("PRAGMA synchronous").fetchone()[0]
            if journal != "delete" or synchronous != 2:
                raise sqlite3.DatabaseError("catalog durability contract failed")
            state = database.execute(
                "SELECT active_generation_id FROM catalog_state WHERE singleton=1"
            ).fetchone()
            active = None if state is None else state[0]
            if not isinstance(active, str) or not active:
                return _generation_result(
                    "ok",
                    "Evidence generation has not been activated; legacy retrieval remains available.",
                    catalog="valid",
                    catalog_schema="valid",
                    freshness="missing",
                    repairable=True,
                    recommended_action="rebuild_generation",
                )
            registered = database.execute(
                "SELECT 1 FROM generations WHERE generation_id=?",
                (active,),
            ).fetchone()
            if registered is None:
                raise sqlite3.DatabaseError("active generation is not registered")
            invalid_details.update(
                catalog="valid",
                active_generation=active,
                catalog_schema="valid",
            )
        if _deadline_reached(deadline):
            raise TimeoutError("generation check deadline")

        import generation_catalog

        generation_path = state_root / "cache" / "evidence-graph" / "generations" / active
        diagnostic_value = json.loads(
            read_runtime_bytes(
                generation_path / "manifest.json",
                state_root,
                max_bytes=MAX_MANIFEST_BYTES,
            )
        )
        if isinstance(diagnostic_value, dict):
            diagnostic_manifest = diagnostic_value
            invalid_details["generation_schema"] = diagnostic_manifest.get("graph_schema_version")
        manifest, seal = generation_catalog._validate_generation(  # noqa: SLF001
            generation_path,
            state_root,
            deadline=deadline,
        )
        source_manifest_raw = read_runtime_bytes(
            generation_path / "source-manifest.json",
            state_root,
            max_bytes=MAX_MANIFEST_BYTES * 1024,
        )
        source_manifest = json.loads(source_manifest_raw)
        policy = source_manifest["policy"]

        from corpus_snapshot import COLLECTOR_VERSION, EXTRACTOR_VERSION, collect_corpus
        from repository_scope import resolve_repository_scope

        repository_scope = resolve_repository_scope(
            root,
            deadline=deadline,
            cancelled=cancelled,
        )
        scope_state = (
            "current"
            if manifest.get("repository_scope") == repository_scope.as_dict()
            else "missing"
            if "repository_scope" not in manifest
            else "mismatched"
        )
        corpus_extraction_state = (
            "current"
            if manifest.get("collector_version") == COLLECTOR_VERSION
            and manifest.get("extractor_version") == EXTRACTOR_VERSION
            else "stale"
        )
        graph_extraction_state = (
            "current"
            if manifest.get("graph_extractor_version") == _maintenance_extractor_identity()
            else "stale"
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
        indexed = {item["relative_path"]: item["sha256"] for item in source_manifest["sources"]}
        current = {source.record.relative_path: source.record.sha256 for source in snapshot.sources}
        delta = sum(
            indexed.get(path) != current.get(path) for path in indexed.keys() | current.keys()
        )
        graph = generation_path / "evidence.sqlite3"
        with _readonly_database(
            graph, state_root, max_bytes=16 * 1024 * 1024 * 1024, deadline=deadline
        ) as database:
            database.set_progress_handler(lambda: int(_deadline_reached(deadline)), 1000)
            unresolved = database.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM observation LIMIT ?)",
                (MAX_OPERATIONAL_ROWS + 1,),
            ).fetchone()[0]
        manifest_seal = next((entry for entry in seal if entry.path == "manifest.json"), None)
        if manifest_seal is not None:
            age_timestamp_ns = manifest_seal.mtime_ns
            age_source = "manifest_mtime"
        elif catalog_info is not None:
            age_timestamp_ns = catalog_info.st_mtime_ns
            age_source = "catalog_mtime"
        else:
            raise OSError("generation age timestamp is unavailable")
        now_ns = int(_as_utc(now).timestamp() * 1_000_000_000)
        age = max(0, (now_ns - age_timestamp_ns) // 1_000_000_000)
        stale_age = age > GENERATION_FRESH_SECONDS
        vector_state = str(manifest["vector_state"])
        complete_v2 = manifest.get("schema_version") == "corpus-generation/v2"
        identity_stale = (
            scope_state != "current"
            or corpus_extraction_state != "current"
            or graph_extraction_state != "current"
            or not complete_v2
        )
        status = (
            "degraded"
            if delta or stale_age or identity_stale or vector_state == "stale" or unresolved
            else "ok"
        )
        return _generation_result(
            status,
            "Evidence generation requires refresh."
            if status != "ok"
            else "Evidence generation is healthy.",
            catalog="valid",
            active_generation=active,
            catalog_schema="valid",
            generation_schema=manifest["graph_schema_version"],
            source_manifest="valid",
            evidence_integrity="valid",
            search_index="valid" if complete_v2 else "missing",
            search_schema="corpus-search/v1" if complete_v2 else None,
            search_integrity="valid" if complete_v2 else "missing",
            vector_state=vector_state,
            vector_model=manifest["embedding_model_id"],
            vector_dimensions=manifest["vector_dimensions"],
            freshness="stale" if delta or stale_age or identity_stale else "fresh",
            repository_scope=scope_state,
            extraction_identity=graph_extraction_state,
            corpus_extraction_identity=corpus_extraction_state,
            unindexed_delta=delta,
            unresolved_observations=unresolved,
            age_seconds=age,
            age_source=age_source,
            repairable=status == "degraded",
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
        if (
            generation_path is not None
            and diagnostic_manifest is not None
            and diagnostic_manifest.get("schema_version") == "corpus-generation/v2"
        ):
            invalid_details["search_schema"] = "corpus-search/v1"
            search_kind = _safe_kind(generation_path / "search.sqlite3", state_root)[0]
            if search_kind == "missing":
                invalid_details.update(search_index="missing", search_integrity="missing")
            elif search_kind == "regular":
                try:
                    from search_memory import validate_generation_fts_artifact

                    validate_generation_fts_artifact(
                        generation_path,
                        diagnostic_manifest,
                        state_root=state_root,
                        deadline=deadline,
                    )
                except (OSError, PermissionError, TypeError, ValueError, sqlite3.Error):
                    invalid_details.update(search_index="corrupt", search_integrity="invalid")
                else:
                    invalid_details.update(search_index="valid", search_integrity="valid")
            else:
                invalid_details.update(search_index="corrupt", search_integrity="invalid")
        return _generation_result(
            "error",
            "Evidence generation catalog or active artifacts are invalid.",
            **invalid_details,
        )


def _index_check(
    state_root: Path,
    now: datetime,
    deadline: float = float("inf"),
    *,
    root: Path | None = None,
) -> dict:
    index = state_root / "cache" / "index.sqlite"
    kind, info = _safe_kind(index, state_root)
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
    if kind != "regular" or info is None or info.st_size == 0:
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
    if time.monotonic() >= deadline:
        return _index_deferred("FTS index check exceeded its time budget.", "budget_exhausted")
    try:
        connection = _readonly_database(
            index,
            state_root,
            max_bytes=MAX_INDEX_DB_BYTES,
            deadline=deadline,
        )
        try:
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            quick_check = connection.execute("PRAGMA quick_check(1)").fetchone()
            if not quick_check or quick_check[0] != "ok":
                raise sqlite3.DatabaseError("quick_check failed")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(pages)")}
            if not INDEX_COLUMNS.issubset(columns):
                raise sqlite3.DatabaseError("unexpected index schema")
            connection.execute(
                "SELECT rowid FROM pages WHERE pages MATCH ? LIMIT 1",
                ("__doctor_integrity_probe__",),
            ).fetchone()
            cursor = connection.execute("SELECT path FROM pages")
            indexed_rows = cursor.fetchmany(MAX_INDEX_PATHS + 1)
        finally:
            connection.close()
        if len(indexed_rows) > MAX_INDEX_PATHS:
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
        indexed_paths = [row[0] for row in indexed_rows]
        if any(not isinstance(item, str) for item in indexed_paths):
            raise ValueError("invalid indexed path")
        manifest = state_root / "cache" / ".paths-manifest"
        manifest_kind, _ = _safe_kind(manifest, state_root)
        manifest_state = "missing"
        manifest_matches_index = False
        if manifest_kind != "missing":
            manifest_paths, manifest_error = _read_bounded_json(
                manifest,
                state_root,
                max_bytes=MAX_MANIFEST_BYTES,
                expected_type=list,
                deadline=deadline,
            )
            if manifest_error == "budget":
                return _index_deferred(
                    "FTS manifest check exceeded its time budget.",
                    "budget_exhausted",
                )
            if manifest_error or any(not isinstance(item, str) for item in (manifest_paths or [])):
                manifest_state = "invalid"
            else:
                manifest_state = "current"
                manifest_matches_index = sorted(manifest_paths) == sorted(indexed_paths)
                if not manifest_matches_index:
                    manifest_state = "mismatch"
        timestamp = datetime.fromtimestamp(info.st_mtime, tz=timezone.utc)
    except sqlite3.OperationalError as exc:
        lowered = str(exc).lower()
        if time.monotonic() >= deadline or "interrupted" in lowered:
            return _index_deferred("FTS index check exceeded its time budget.", "budget_exhausted")
        if "locked" in lowered or "busy" in lowered:
            return _index_deferred("FTS index is busy.", "database_busy")
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
    except (OSError, OverflowError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error):
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
    source_root = Path(root or state_root)
    try:
        import search_memory

        pages = search_memory._collect_pages(
            "all",
            knowledge_dir=source_root / "knowledge" / "notes",
            root=source_root,
            deadline=deadline,
        )
        source_rebuild_required = search_memory._needs_rebuild(
            pages,
            root=source_root,
            index_file=index,
            index_manifest=manifest,
            deadline=deadline,
        )
    except (OSError, ValueError, sqlite3.Error):
        return _index_deferred(
            "FTS source freshness could not be determined.",
            "source_freshness_unknown",
        )
    age = max(0, int((now - timestamp).total_seconds()))
    if source_rebuild_required or not manifest_matches_index:
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
    freshness = "fresh" if age <= INDEX_FRESH_SECONDS else "stale"
    status = "ok" if freshness == "fresh" else "degraded"
    message = "FTS index is fresh." if status == "ok" else "FTS index is stale."
    return _result(
        "index",
        status,
        message,
        {
            "exists": True,
            "freshness": freshness,
            "age_seconds": age,
            "repairable": freshness == "stale",
            "source_rebuild_required": False,
            "source_contract": "path-manifest+mtime",
            "manifest": manifest_state,
        },
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
    command = table.get("command")
    args = table.get("args")
    enabled = table.get("enabled", True)
    configured = (
        command == "uv"
        and isinstance(args, list)
        and all(isinstance(item, str) for item in args)
        and len(args) == 8
        and args[:4] == ["run", "--locked", "--no-sync", "--directory"]
        and args[5:] == ["python", "scripts/mcp_server.py"]
        and enabled is True
    )
    return configured, "configured" if configured else "target_missing_or_invalid"


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


def _probe_codex_hooks_list(
    root: Path, home: Path, *, deadline: float = float("inf")
) -> dict[str, Any] | object | None:
    started_at = time.monotonic()
    probe_deadline = min(deadline, started_at + CODEX_HOOK_PROBE_SECONDS)

    def deadline_result() -> object | None:
        return _CODEX_PROBE_NOT_COMPLETED if _deadline_reached(deadline) else None

    if probe_deadline - started_at < CODEX_HOOK_PROBE_STARTUP_SECONDS:
        return None
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
    payload = "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in requests).encode(
        "utf-8"
    )
    command = _codex_app_server_command()
    if command is None:
        return None
    env = os.environ.copy()
    env["CODEX_HOME"] = str(home / ".codex")
    try:
        process = subprocess.Popen(  # noqa: S603
            command,
            cwd=str(root),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            try:
                process.wait(timeout=max(0.0, probe_deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
            return deadline_result()
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
        if _deadline_reached(probe_deadline):
            process.kill()
            try:
                process.wait(timeout=0)
            except subprocess.TimeoutExpired:
                pass
            return deadline_result()
        process.stdin.write(payload)
        process.stdin.flush()
        process.stdin.close()
        try:
            process.wait(timeout=max(0.0, probe_deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=max(0.0, probe_deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
            return deadline_result()
        for reader in readers:
            reader.join(timeout=max(0.0, probe_deadline - time.monotonic()))
        if any(reader.is_alive() for reader in readers):
            if process.returncode is None:
                process.kill()
            return deadline_result()
        if process.returncode != 0 or overflow.is_set():
            return None
        raw = bytes(captured["stdout"])
    except (OSError, PermissionError, subprocess.SubprocessError, ValueError):
        return deadline_result()
    try:
        text = raw.decode("utf-8")
        for line in text.splitlines():
            message = json.loads(line)
            if isinstance(message, dict) and message.get("id") == 2:
                result = message.get("result")
                return result if isinstance(result, dict) else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return None


def _expected_codex_runtime_hooks(template_path: Path) -> list[dict[str, Any]]:
    value, problem = _read_bounded_json(
        template_path,
        template_path.parent,
        max_bytes=MAX_CONFIG_BYTES,
    )
    if problem or not isinstance(value, dict) or not isinstance(value.get("hooks"), dict):
        raise ValueError("invalid Codex hook template")
    expected = []
    for event_name, groups in value["hooks"].items():
        if not isinstance(event_name, str) or not isinstance(groups, list) or len(groups) != 1:
            raise ValueError("invalid Codex hook template")
        group = groups[0]
        handlers = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(handlers, list) or len(handlers) != 1:
            raise ValueError("invalid Codex hook template")
        handler = handlers[0]
        if not isinstance(handler, dict):
            raise ValueError("invalid Codex hook template")
        command_key = "commandWindows" if os.name == "nt" else "command"
        command = handler.get(command_key)
        if not isinstance(command, str):
            raise ValueError("invalid Codex hook template")
        expected.append(
            {
                "eventName": event_name,
                "matcher": group.get("matcher"),
                "command": command,
            }
        )
    return expected


def _codex_runtime_hooks_state(
    root: Path, home: Path, *, deadline: float = float("inf")
) -> tuple[bool, str]:
    if deadline - time.monotonic() < CODEX_HOOK_PROBE_STARTUP_SECONDS:
        return False, "runtime_hooks_not_completed"
    response = _probe_codex_hooks_list(root, home, deadline=deadline)
    if response is _CODEX_PROBE_NOT_COMPLETED:
        return False, "runtime_hooks_not_completed"
    if response is None:
        return False, "runtime_hooks_unverified"
    assert isinstance(response, dict)
    data = response.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        return False, "runtime_hooks_invalid"
    entry = data[0]
    try:
        if Path(entry.get("cwd", "")).resolve() != root.resolve():
            return False, "runtime_hooks_wrong_cwd"
    except (OSError, TypeError, ValueError):
        return False, "runtime_hooks_invalid"
    if entry.get("warnings") or entry.get("errors"):
        return False, "runtime_hooks_warning_or_error"
    hooks = entry.get("hooks")
    if not isinstance(hooks, list) or any(not isinstance(item, dict) for item in hooks):
        return False, "runtime_hooks_invalid"
    try:
        expected = _expected_codex_runtime_hooks(root / "integrations" / "codex" / "hooks.json")
    except ValueError:
        return False, "runtime_hooks_template_invalid"
    ours = [
        hook
        for hook in hooks
        if isinstance(hook.get("command"), str)
        and "codex_memory.py" in hook["command"]
        and hook["command"].rstrip().endswith(" hook")
    ]
    if len(ours) != len(expected):
        return False, "runtime_hooks_mismatch"
    for wanted in expected:
        matches = [
            hook
            for hook in ours
            if all(hook.get(field) == value for field, value in wanted.items())
        ]
        if len(matches) != 1:
            return False, "runtime_hooks_mismatch"
        hook = matches[0]
        if hook.get("enabled") is not True:
            return False, "runtime_hooks_disabled"
        trust = hook.get("trustStatus")
        if trust not in {"trusted", "managed"}:
            return False, f"runtime_hooks_{trust}" if trust in {
                "untrusted",
                "modified",
            } else "runtime_hooks_trust_unknown"
    return True, "runtime_hooks_active"


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
            [(home / ".claude" / "settings.json", ("LLM_WIKI_ROOT", "session_start_context.py"))],
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


def _lsp_pid_state(pid: int) -> str:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return "unknown"
    if sys.platform == "win32":
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
                return "dead" if ctypes.get_last_error() in {87, 1168} else "unknown"
            try:
                exit_code = wintypes.DWORD()
                if not get_exit_code(handle, ctypes.byref(exit_code)):
                    return "unknown"
                return "alive" if exit_code.value == 259 else "dead"
            finally:
                close_handle(handle)
        except (AttributeError, OSError, OverflowError, ValueError):
            return "unknown"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "unknown"
    except OSError as exc:
        return "dead" if exc.errno == errno.ESRCH else "unknown"
    except (OverflowError, ValueError):
        return "unknown"
    return "alive"


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


def _acquire_lock(path: Path, root: Path, now: datetime) -> str | None:
    token = secrets.token_hex(16)
    if _create_owned_lock(path, token, now):
        return token
    fd = _open_existing_lock(path, root)
    if fd is None:
        return None
    try:
        if not _lock_file_nonblocking(fd):
            return None
        existing, opened_stat = _read_lock_fd(fd)
        if existing is None or opened_stat is None:
            return None
        pid = existing.get("lock_pid")
        old_token = existing.get("lock_token")
        try:
            acquired = datetime.fromisoformat(str(existing.get("lock_acquired_at", "")))
            if acquired.tzinfo is None:
                acquired = acquired.replace(tzinfo=timezone.utc)
            stale = (now - acquired.astimezone(timezone.utc)).total_seconds() > LOCK_STALE_SECONDS
        except (TypeError, ValueError):
            return None
        if (
            not stale
            or not isinstance(pid, int)
            or pid <= 0
            or not isinstance(old_token, str)
            or not old_token
            or _pid_alive(pid)
        ):
            return None
        current_value, current_opened_stat = _read_lock_fd(fd)
        if (
            current_value is None
            or current_opened_stat is None
            or current_value.get("lock_token") != old_token
        ):
            return None
        try:
            current_path_stat = os.stat(path, follow_symlinks=False)
        except OSError:
            return None
        if not os.path.samestat(opened_stat, current_path_stat):
            return None
        quarantine = path.with_name(f"{path.name}.stale-{token}")
        try:
            path.rename(quarantine)
        except OSError:
            return None
        try:
            return token if _create_owned_lock(path, token, now) else None
        finally:
            try:
                quarantine.unlink()
            except OSError:
                pass
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
    recovered = 0
    try:
        with os.scandir(queue) as entries:
            for number, entry in enumerate(entries):
                if number >= MAX_QUEUE_FILES:
                    break
                if not entry.name.endswith(".processing"):
                    continue
                lease = Path(entry.path)
                task, problem = _read_bounded_json(lease, queue)
                if problem or task is None:
                    continue
                stale, owned = _lease_state(task, now)
                pid = task.get("lease_pid")
                if not stale or not owned or _pid_alive(pid):
                    continue
                target = lease.with_suffix(".json")
                try:
                    os.link(lease, target, follow_symlinks=False)
                    lease.unlink()
                    recovered += 1
                except (FileExistsError, OSError):
                    continue
    finally:
        _release_lock(lock, queue, lock_token)
    if recovered:
        repaired.append({"action": "recover_stale_lease", "count": recovered})
    return True


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
        root_resolved = root.resolve()
        knowledge_path = root / "knowledge"
        notes_path = knowledge_path / "notes"
        for component in (knowledge_path, notes_path):
            try:
                component_info = component.lstat()
            except OSError as exc:
                raise OSError("unsafe knowledge path") from exc
            if stat.S_ISLNK(component_info.st_mode) or not stat.S_ISDIR(component_info.st_mode):
                raise OSError("unsafe knowledge path")
        notes = notes_path.resolve()
        try:
            notes.relative_to(root_resolved)
        except ValueError as exc:
            raise OSError("unsafe knowledge path") from exc
        pages = []
        for page in sorted(notes.rglob("*.md")):
            if _deadline_reached(deadline) or bool(cancelled and cancelled()):
                raise TimeoutError("index rebuild cancelled or deadline reached")
            kind, _ = _safe_kind(page, notes)
            if kind == "regular":
                pages.append(page)
        if _deadline_reached(deadline) or bool(cancelled and cancelled()):
            raise TimeoutError("index rebuild cancelled or deadline reached")
        search_memory._build_index(pages)
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
    if (
        _safe_kind(graph_root / "catalog.sqlite3", state_root)[0] == "missing"
        and _safe_kind(graph_root / "generations", state_root)[0] == "missing"
    ):
        return
    catalog = generation_catalog.GenerationCatalog(state_root)
    with closing(
        catalog._readonly()  # noqa: SLF001 - repair needs pointer comparison
    ) as database:
        row = database.execute(
            "SELECT active_generation_id FROM catalog_state WHERE singleton=1"
        ).fetchone()
        active_before = None if row is None else row[0]
    recovered = catalog.recover_orphans(deadline=deadline)
    if recovered:
        repaired.append({"action": "recover_generation_orphans", "count": len(recovered)})

    active_manifest = catalog.get_active(deadline=deadline)
    active_after = None if active_manifest is None else str(active_manifest["generation_id"])
    if active_after != active_before:
        repaired.append(
            {
                "action": "fallback_generation",
                "from": active_before,
                "to": active_after,
            }
        )

    with closing(catalog._readonly()) as database:  # noqa: SLF001 - bounded catalog repair
        rows = database.execute(
            "SELECT generation_id FROM generations LIMIT ?",
            (generation_catalog.MAX_GENERATIONS + 1,),
        ).fetchall()
    if len(rows) > generation_catalog.MAX_GENERATIONS:
        raise ValueError("generation catalog exceeds cleanup bound")
    registered = {str(row[0]) for row in rows}
    removed = 0
    children = generation_catalog._bounded_scandir(  # noqa: SLF001
        catalog.generations_path,
        generation_catalog.MAX_GENERATION_CHILDREN,
        "generation child count exceeds cleanup bound",
        deadline=deadline,
        cancelled=cancelled,
    )
    for entry in children:
        if bool(cancelled and cancelled()) or _deadline_reached(deadline):
            raise TimeoutError("generation cleanup deadline reached")
        path = Path(entry.path)
        if entry.name in registered or not entry.is_dir(follow_symlinks=False):
            continue
        try:
            generation_catalog._generation_id(entry.name)  # noqa: SLF001
            if generation_catalog._is_link_or_reparse(path):  # noqa: SLF001
                continue
            generation_catalog._validate_generation(  # noqa: SLF001
                path,
                state_root,
                deadline=deadline,
                cancelled=cancelled,
            )
        except TimeoutError:
            raise
        except (FileNotFoundError, OSError, PermissionError, TypeError, ValueError):
            if path.parent.resolve(strict=True) != catalog.generations_path.resolve(strict=True):
                continue
            shutil.rmtree(path)
            removed += 1
    if removed:
        repaired.append({"action": "cleanup_generation_orphans", "count": removed})


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

    def check_stop() -> None:
        if cancelled is not None and cancelled():
            raise TimeoutError("workspace extraction partition cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("workspace extraction partition deadline reached")

    check_stop()
    occurrence_sources: dict[str, set[str]] = {}
    record_sources: dict[str, str] = {}
    node_references: dict[str, set[str]] = {}
    dependencies = {source_id: set() for source_id in source_ids}
    workspace_sensitive = set()
    for occurrence in result.occurrences:
        check_stop()
        source_id = str(occurrence["source_id"])
        node_id = str(occurrence["node_id"])
        occurrence_sources.setdefault(node_id, set()).add(source_id)
        node_references.setdefault(node_id, set()).add(source_id)
        grouped[source_id]["occurrences"].append(occurrence)
    for evidence in result.evidence:
        check_stop()
        source_id = str(evidence["source_id"])
        grouped[source_id]["evidence"].append(evidence)
        assertion_id = evidence.get("assertion_id")
        observation_id = evidence.get("observation_id")
        if assertion_id is not None:
            record_sources[str(assertion_id)] = source_id
        if observation_id is not None:
            record_sources[str(observation_id)] = source_id
    for assertion in result.assertions:
        check_stop()
        owner = record_sources[str(assertion["assertion_id"])]
        grouped[owner]["assertions"].append(assertion)
        node_references.setdefault(str(assertion["source_node_id"]), set()).add(owner)
        target = assertion.get("target_node_id")
        if target is not None:
            node_references.setdefault(str(target), set()).add(owner)
            dependencies[owner].update(occurrence_sources.get(str(target), ()))
    for observation in result.observations:
        check_stop()
        owner = record_sources[str(observation["observation_id"])]
        grouped[owner]["observations"].append(observation)
        source_node = observation.get("source_node_id")
        if source_node is not None:
            node_references.setdefault(str(source_node), set()).add(owner)
        observation_dependencies = getattr(result, "observation_source_dependencies", {})
        if (
            observation["reason"] in {"missing_dependency", "unresolved_reference"}
            and str(observation["observation_id"]) not in observation_dependencies
        ):
            workspace_sensitive.add(owner)

    for observation_id, candidate_sources in getattr(
        result, "observation_source_dependencies", {}
    ).items():
        check_stop()
        dependencies[record_sources[str(observation_id)]].update(candidate_sources)
    for dependency in getattr(result, "dependencies", ()):
        check_stop()
        owner = dependency.get("source_id")
        owners = (
            (str(owner),)
            if owner is not None
            else tuple(sorted(occurrence_sources.get(str(dependency["dependent_node_id"]), ())))
        )
        for source_id in owners:
            grouped[source_id]["dependencies"].append(dependency)

    fallback_owner = min(source_ids)
    for node in result.nodes:
        check_stop()
        node_id = str(node["node_id"])
        owners = occurrence_sources.get(node_id)
        if not owners:
            owners = node_references.get(node_id, {fallback_owner})
        for source_id in sorted(owners):
            grouped[source_id]["nodes"].append(node)
    for source_id in source_ids:
        check_stop()
        dependencies[source_id].discard(source_id)

    partitions = {}
    for source_id in source_ids:
        check_stop()
        records = grouped[source_id]
        partitions[source_id] = SourceExtraction(
            nodes=tuple(records["nodes"]),
            occurrences=tuple(records["occurrences"]),
            assertions=tuple(records["assertions"]),
            evidence=tuple(records["evidence"]),
            observations=tuple(records["observations"]),
            dependencies=tuple(records["dependencies"]),
            source_dependencies=tuple(sorted(dependencies[source_id])),
            workspace_sensitive=source_id in workspace_sensitive,
        )
    return partitions


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
    from corpus_snapshot import (
        APPROVED_CODE_ROOTS,
        collect_corpus,
    )
    from evidence_graph_builder import (
        GRAPH_SCHEMA_VERSION,
        IncrementalReuseConfig,
        build_incremental_generation,
    )
    from generation_catalog import GenerationCatalog
    from reliable_memory import canonical_json_bytes
    from repository_scope import resolve_repository_scope

    repository_scope = resolve_repository_scope(
        root,
        deadline=deadline,
        cancelled=cancelled,
    )
    maintenance_extractor_version = _maintenance_extractor_identity()

    code_roots = tuple(
        relative for relative in sorted(APPROVED_CODE_ROOTS) if (root / relative).is_dir()
    )
    snapshot = collect_corpus(
        root,
        code_roots=code_roots,
        max_files=max_sources,
        deadline=deadline,
    )
    if len(snapshot.sources) > max_sources:
        raise ValueError("corpus source limit exceeded")
    catalog = GenerationCatalog(state_root)
    parent = catalog.get_active(deadline=deadline)
    parent_id = None if parent is None else str(parent["generation_id"])
    source_manifest_sha256 = snapshot.corpus_sha256
    policy = {
        "daily_paths": list(snapshot.policy.daily_paths),
        "code_roots": list(snapshot.policy.code_roots),
        "include_historical": snapshot.policy.include_historical,
        "as_of": snapshot.policy.as_of,
    }
    workspace_membership = sorted(
        [
            source.record.logical_id,
            source.record.relative_path,
            source.record.language,
        ]
        for source in snapshot.sources
        if not source.record.relative_path.startswith("knowledge/")
    )
    workspace_manifest_sha256 = hashlib.sha256(
        canonical_json_bytes(workspace_membership)
    ).hexdigest()
    parent_workspace_manifest_sha256 = None
    if parent_id is not None:
        from evidence_graph_builder import _load_incremental_manifest

        parent_incremental, _parent_generation = _load_incremental_manifest(
            catalog,
            parent_id,
            deadline=deadline,
            cancelled=cancelled,
        )
        if parent_incremental is not None:
            parent_workspace_manifest_sha256 = parent_incremental["reuse_config"].get(
                "workspace_manifest_sha256"
            )
    if (
        parent is not None
        and not force_rebuild
        and parent.get("schema_version") == "corpus-generation/v2"
        and parent.get("repository_scope") == repository_scope.as_dict()
        and parent.get("collector_version") == snapshot.collector_version
        and parent.get("extractor_version") == snapshot.extractor_version
        and parent.get("graph_extractor_version") == maintenance_extractor_version
        and parent.get("source_manifest_sha256") == source_manifest_sha256
        and parent_workspace_manifest_sha256 == workspace_manifest_sha256
    ):
        return {
            "status": "current",
            "generation_id": parent_id,
            "sources": len(snapshot.sources),
            "partial": False,
        }
    config = IncrementalReuseConfig(
        extractor_version=maintenance_extractor_version,
        grammar_version="builtin-grammars/v1",
        compiler_version=f"python-{sys.version_info.major}.{sys.version_info.minor}",
        resolver_config_sha256=hashlib.sha256(b"llm-wiki-maintenance-resolver/v1").hexdigest(),
        schema_version=GRAPH_SCHEMA_VERSION,
        workspace_manifest_sha256=workspace_manifest_sha256,
    )
    source_rows = [
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
    source_bytes = {source.record.logical_id: source.content for source in snapshot.sources}
    while True:
        generation_id = f"generation-{time.time_ns():x}-{secrets.token_hex(4)}"
        if not (catalog.generations_path / generation_id).exists():
            break
    built = build_incremental_generation(
        catalog,
        sources=source_rows,
        source_bytes=source_bytes,
        extractor=_generation_source_extractor(snapshot, repository_scope.repository_id),
        reuse_config=config,
        generation_id=generation_id,
        parent_generation_id=None if force_rebuild else parent_id,
        policy=policy,
        expected_active=parent_id,
        deadline=deadline,
        cancelled=cancelled,
        repository_scope=repository_scope,
        snapshot=snapshot,
        publication_root=root,
        coordinator=coordinator,
    )
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


def run_generation_maintenance(
    root: Path | str | None = None,
    state_root: Path | str | None = None,
    *,
    time_budget_seconds: float = DEFAULT_GENERATION_TIME_BUDGET_SECONDS,
    max_sources: int = DEFAULT_GENERATION_SOURCE_LIMIT,
    force_rebuild: bool = False,
) -> dict:
    """Run one bounded fenced generation refresh; never mutate knowledge."""
    if (
        isinstance(time_budget_seconds, bool)
        or not isinstance(time_budget_seconds, (int, float))
        or not math.isfinite(time_budget_seconds)
        or time_budget_seconds <= 0
    ):
        raise ValueError("time_budget_seconds must be positive and finite")
    if isinstance(max_sources, bool) or not isinstance(max_sources, int) or max_sources < 1:
        raise ValueError("max_sources must be a positive integer")
    root_path = Path(
        root or os.environ.get("LLM_WIKI_ROOT", Path(__file__).resolve().parent.parent)
    ).resolve()
    state_path = Path(
        os.path.abspath(state_root or os.environ.get("LLM_WIKI_STATE_ROOT", root_path))
    )
    deadline = time.monotonic() + float(time_budget_seconds)
    filesystem = _filesystem_check(state_path, deadline)
    if filesystem["status"] == "error":
        if filesystem["details"].get("budget_exhausted"):
            return {
                "status": "deferred",
                "generation_id": None,
                "sources": 0,
                "partial": True,
                "reason": "time_limit",
            }
        return {
            "status": "error",
            "generation_id": None,
            "sources": 0,
            "partial": False,
            "reason": "unsupported_filesystem",
        }
    acquired = _acquire_maintenance_owner(root_path, state_path, datetime.now(timezone.utc))
    if acquired is None:
        return {
            "status": "deferred",
            "generation_id": None,
            "sources": 0,
            "partial": True,
            "reason": "maintenance_owner_busy",
        }
    coordinator, lease = acquired
    repaired: list[dict] = []
    try:
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
    except TimeoutError:
        return {
            "status": "deferred",
            "generation_id": None,
            "sources": 0,
            "partial": True,
            "reason": "time_limit",
            "repairs": repaired,
        }
    except ValueError as exc:
        if "limit" in str(exc).casefold() or "ceiling" in str(exc).casefold():
            return {
                "status": "deferred",
                "generation_id": None,
                "sources": 0,
                "partial": True,
                "reason": "source_limit",
                "repairs": repaired,
            }
        return {
            "status": "error",
            "generation_id": None,
            "sources": 0,
            "partial": False,
            "reason": type(exc).__name__,
            "repairs": repaired,
        }
    except (OSError, PermissionError, sqlite3.Error) as exc:
        return {
            "status": "error",
            "generation_id": None,
            "sources": 0,
            "partial": False,
            "reason": type(exc).__name__,
            "repairs": repaired,
        }


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
    selected_repairs = set(VALID_REPAIR_ACTIONS if repair_actions is None else repair_actions)
    unknown_repairs = selected_repairs - VALID_REPAIR_ACTIONS
    if unknown_repairs:
        raise ValueError(f"unknown doctor repair actions: {sorted(unknown_repairs)}")
    root_path = Path(
        root or os.environ.get("LLM_WIKI_ROOT", Path(__file__).resolve().parent.parent)
    ).resolve()
    state_path = Path(
        os.path.abspath(state_root or os.environ.get("LLM_WIKI_STATE_ROOT", root_path))
    )
    home_path = Path(home).resolve() if home is not None else Path.home().resolve()
    generated_at = _as_utc(now)
    repaired: list[dict] = []
    repair_errors: dict[str, list[str]] = {}
    repair_deferred: set[str] = set()
    if deadline is None:
        deadline = time.monotonic() + max(0.0, time_budget_seconds)
    elif (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
        raise ValueError("deadline must be a finite monotonic timestamp")
    else:
        deadline = float(deadline)

    if repair:
        maintenance: tuple[Any, dict[str, object]] | None = None
        guard_entered = False
        try:
            maintenance = _acquire_maintenance_owner(root_path, state_path, generated_at)
            if maintenance is None:
                deferred_by_action = {
                    "runtime": {"runtime"},
                    "transactions": {"transactions"},
                    "queue": {"queue"},
                    "indexes": {"index", "claims"},
                    "archives": {"archives"},
                    "generations": {"generation"},
                }
                for action in selected_repairs:
                    repair_deferred.update(deferred_by_action[action])
            else:
                coordinator, lease = maintenance
                with _MaintenanceHeartbeat(coordinator, lease, deadline=deadline) as guard:
                    guard_entered = True
                    if "runtime" in selected_repairs:
                        guard.run(_repair_runtime, state_path, repaired)
                    if "generations" in selected_repairs:
                        guard.run(
                            _repair_generation_catalog,
                            root_path,
                            state_path,
                            deadline=deadline,
                            cancelled=guard.cancelled,
                            repaired=repaired,
                        )
                        if rebuild_generation:
                            generation_result = guard.run(
                                _build_or_refresh_generation,
                                root_path,
                                state_path,
                                deadline=deadline,
                                cancelled=guard.cancelled,
                                max_sources=DEFAULT_GENERATION_SOURCE_LIMIT,
                                force_rebuild=True,
                            )
                            if generation_result["status"] == "built":
                                repaired.append(
                                    {
                                        "action": "rebuild_generation",
                                        "generation_id": generation_result["generation_id"],
                                    }
                                )
                            elif generation_result["status"] == "deferred":
                                repair_deferred.add("generation")
                    if "transactions" in selected_repairs:
                        recovered = guard.run(
                            coordinator.recover,
                            writer_wait_seconds=0,
                            max_transactions=MAX_OPERATIONAL_ROWS,
                            deadline=deadline,
                            cancelled=guard.cancelled,
                        )
                        if recovered:
                            repaired.append(
                                {
                                    "action": "recover_transactions",
                                    "count": len(recovered),
                                }
                            )
                    queue_v2_ready = False
                    if "queue" in selected_repairs:
                        legacy_available = guard.run(
                            _repair_leases, state_path, generated_at, repaired
                        )
                        if not legacy_available:
                            repair_deferred.add("queue")
                        from memory_queue import MemoryQueue, migrate_legacy_queue

                        marker = state_path / "run" / "queue-migrated-v2"
                        marker_existed = _safe_kind(marker, state_path)[0] == "regular"
                        migration = (
                            guard.run(
                                migrate_legacy_queue,
                                state_path,
                                deadline=deadline,
                                cancelled=guard.cancelled,
                            )
                            if legacy_available
                            else None
                        )
                        if migration is not None and (
                            not marker_existed or migration.imported or migration.quarantined
                        ):
                            repaired.append(
                                {
                                    "action": "migrate_queue",
                                    "count": migration.imported + migration.quarantined,
                                }
                            )
                        marker_valid = _safe_kind(marker, state_path)[0] == "regular"
                        if migration is not None or marker_valid:
                            guard.run(MemoryQueue, state_path)
                        queue_v2_ready = migration is not None or marker_valid
                    if "indexes" in selected_repairs:
                        index_before = _index_check(
                            state_path, generated_at, deadline, root=root_path
                        )
                        if (
                            index_before["details"].get("repairable")
                            and index_before["status"] != "ok"
                        ):
                            index_lock = state_path / "cache" / ".doctor-index.lock"
                            lock_token = guard.run(
                                _acquire_lock,
                                index_lock,
                                state_path / "cache",
                                generated_at,
                            )
                            if lock_token is None:
                                repair_deferred.add("index")
                            else:
                                try:
                                    guard.run(
                                        _rebuild_index,
                                        root_path,
                                        state_path,
                                        deadline=deadline,
                                        cancelled=guard.cancelled,
                                    )
                                    index_after = _index_check(
                                        state_path,
                                        generated_at,
                                        deadline,
                                        root=root_path,
                                    )
                                    if index_after["status"] == "ok":
                                        repaired.append({"action": "rebuild_index"})
                                    else:
                                        message = (
                                            "Index repair failed: index was not created"
                                            if index_after["details"].get("freshness") == "missing"
                                            else "Index repair failed: rebuilt index did not validate as fresh"
                                        )
                                        repair_errors.setdefault("index", []).append(message)
                                except Exception as exc:  # noqa: BLE001
                                    repair_errors.setdefault("index", []).append(
                                        f"Index repair failed: {type(exc).__name__}"
                                    )
                                finally:
                                    guard.cleanup(
                                        _release_lock,
                                        index_lock,
                                        state_path / "cache",
                                        lock_token,
                                    )
                    if "archives" in selected_repairs:
                        archive_before = _archive_check(root_path, state_path, deadline)
                        archive_root = _archive_path(root_path)
                        if (
                            _safe_kind(archive_root, root_path)[0] == "directory"
                            and archive_before["status"] != "ok"
                        ):
                            from archive_daily import DailyArchiver

                            guard.run(
                                lambda: DailyArchiver(root_path, state_path).recover(
                                    deadline=deadline,
                                    cancelled=guard.cancelled,
                                )
                            )
                            repaired.append({"action": "recover_archives"})
                    if "indexes" in selected_repairs:
                        claim_before = _claim_check(root_path, state_path, deadline)
                        if claim_before["status"] != "ok":
                            from claims import ClaimIndex

                            sources = [root_path / "knowledge" / "notes"]
                            projects = root_path / "knowledge" / "projects"
                            if _safe_kind(projects, root_path)[0] == "directory":
                                sources.append(projects)
                            claim_index = ClaimIndex(state_path, vault=root_path)
                            guard.run(
                                claim_index.rebuild,
                                sources,
                                deadline=deadline,
                                cancelled=guard.cancelled,
                            )
                            repaired.append({"action": "rebuild_claim_index"})
                    if "queue" in selected_repairs:
                        unblocked = (
                            guard.run(_repair_queue_capabilities, state_path)
                            if queue_v2_ready
                            else 0
                        )
                        if unblocked:
                            repaired.append(
                                {
                                    "action": "unblock_capabilities",
                                    "count": unblocked,
                                }
                            )
                        processed = (
                            guard.run(
                                _run_bounded_worker,
                                state_path,
                                deadline=deadline,
                                cancelled=guard.cancelled,
                            )
                            if queue_v2_ready
                            else 0
                        )
                        if processed:
                            repaired.append({"action": "run_bounded_worker", "count": processed})
        except Exception as exc:  # noqa: BLE001
            repair_errors.setdefault("runtime", []).append(f"Repair failed: {type(exc).__name__}")
        finally:
            if maintenance is not None and not guard_entered:
                try:
                    _release_maintenance_owner(*maintenance)
                except Exception as exc:  # noqa: BLE001
                    repair_errors.setdefault("runtime", []).append(
                        f"Maintenance owner release failed: {type(exc).__name__}"
                    )

    checks = [
        _environment_check(root_path, state_path),
        _runtime_check(state_path),
        _filesystem_check(state_path, deadline),
        _transaction_check(state_path, generated_at, deadline),
        _queue_check(state_path, generated_at, deadline),
        _archive_check(root_path, state_path, deadline),
        _claim_check(root_path, state_path, deadline),
    ]
    remaining = (
        (
            "generation",
            lambda: _generation_check(
                root_path,
                state_path,
                generated_at,
                deadline,
            ),
        ),
        (
            "index",
            lambda: _index_check(state_path, generated_at, deadline, root=root_path),
        ),
        (
            "scheduler",
            lambda: _scheduler_check(root_path, state_path, generated_at, deadline),
        ),
        ("capture", lambda: _capture_check(state_path, deadline)),
        ("mcp", lambda: _mcp_check(root_path)),
        (
            "integrations",
            lambda: _integration_check(root_path, home_path, deadline=deadline),
        ),
        (
            "pyright",
            lambda: _pyright_check(root_path, state_path, deadline=deadline),
        ),
        (
            "lsp",
            lambda: _lsp_runtime_check(state_path, generated_at, deadline=deadline),
        ),
    )
    for check_id, operation in remaining:
        if check_id != "lsp" and time.monotonic() >= deadline:
            checks.append(
                _result(
                    check_id,
                    "degraded",
                    "Check not completed because the doctor time budget was exhausted.",
                    {"budget_exhausted": True},
                )
            )
        else:
            checks.append(operation())
    for check in checks:
        if check["id"] in repair_deferred:
            check["status"] = "degraded"
            check["message"] = (
                f"{check['id'].title()} repair deferred because another owner "
                "holds the repair lock."
            )
            check["details"]["repair_deferred"] = True
        if errors := repair_errors.get(check["id"]):
            check["status"] = "error"
            check["message"] = f"{check['id'].title()} repair failed."
            check["details"]["repair_errors"] = errors
    counts = {
        status: sum(check["status"] == status for check in checks) for status in VALID_STATUSES
    }
    overall = "error" if counts["error"] else "degraded" if counts["degraded"] else "ok"
    collected = {check["id"]: check for check in checks}
    run_deletion = _run_deletion_check(
        state_path,
        generated_at,
        root=root_path,
        deadline=deadline,
        collected=collected,
    )
    checks.append(
        _result(
            "run_deletion",
            "ok",
            "Runtime state was observed quiescent; offline action is still required."
            if run_deletion["quiescent"]
            else "Runtime history must be retained.",
            run_deletion,
        )
    )
    counts = {
        status: sum(check["status"] == status for check in checks) for status in VALID_STATUSES
    }
    overall = "error" if counts["error"] else "degraded" if counts["degraded"] else "ok"
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "overall_status": overall,
        "repaired": repaired,
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
