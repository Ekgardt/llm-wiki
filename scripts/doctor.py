"""Agent-readable local health checks and conservative repairs."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import reliable_memory
from bounded_io import read_stable_bytes
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
    {"runtime", "transactions", "queue", "indexes", "archives"}
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
    message = "Configured roots and source layout are available." if status == "ok" else "Configured environment is incomplete."
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


def _runtime_check(state_root: Path) -> dict:
    details = {}
    missing = 0
    unwritable = 0
    unsafe = 0
    for relative in RUNTIME_DIRECTORIES:
        path = state_root / relative
        kind, _ = _safe_kind(path, state_root)
        exists = kind == "directory"
        writable = _is_writable_directory(path) if exists else False
        details[relative] = {
            "exists": kind != "missing",
            "writable": writable,
            "symlink": kind == "symlink",
            "safe": kind in {"missing", "directory"},
        }
        missing += not exists
        unwritable += exists and not writable
        unsafe += kind not in {"missing", "directory"}
    if unsafe:
        status, message = "error", "Runtime paths include unsafe entries."
    elif unwritable:
        status, message = "error", "Runtime directories are not writable."
    elif missing:
        status, message = "degraded", f"{missing} runtime directories are missing."
    else:
        status, message = "ok", "Runtime directories exist and are writable."
    return _result("runtime", status, message, details)


def _read_bounded_json(
    path: Path,
    root: Path,
    *,
    max_bytes: int = MAX_QUEUE_FILE_BYTES,
    expected_type: type = dict,
    deadline: float = float("inf"),
) -> tuple[Any | None, str | None]:
    if time.monotonic() >= deadline:
        return None, "budget"
    try:
        metadata = path.lstat()
        if metadata.st_size > max_bytes:
            return None, "oversized"
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
    if database_kind == "missing" and _database_sidecar_present(
        database_path, state_root
    ):
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
                            permanently_failed += int(task.get("attempts", 0)) >= PERMANENT_FAILURE_ATTEMPTS
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
        status, message = "degraded", f"Queue has {pending} pending task(s) and {stale_leases} stale lease(s)."
    else:
        status, message = "ok", "Queue has no pending or stale work."
    if artifacts["deletion_codes"]:
        if artifacts["artifact_error"]:
            status = "error"
        elif status == "ok":
            status = "degraded"
    return _result("queue", status, message, details)


def _readonly_database(
    path: Path,
    state_root: Path,
    *,
    max_bytes: int = MAX_OPERATIONAL_DB_BYTES,
) -> sqlite3.Connection:
    return open_readonly_operational_db(
        path,
        state_root,
        max_bytes=max_bytes,
        owner_only=False,
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


def _tables(
    database: sqlite3.Connection, deadline: float = float("inf")
) -> set[str]:
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


def _transaction_artifacts(
    state_root: Path, deadline: float
) -> tuple[set[str], bool]:
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


def _transaction_check(
    state_root: Path, now: datetime, deadline: float = float("inf")
) -> dict:
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
        with _readonly_database(path, state_root) as database:
            if _deadline_reached(deadline):
                raise TimeoutError("transaction check deadline")
            tables = _tables(database, deadline)
            if "transaction" not in tables:
                raise sqlite3.DatabaseError("transaction table missing")
            transaction_columns = _columns(database, "transaction", deadline)
            operation_columns = (
                _columns(database, "operation", deadline)
                if "operation" in tables
                else set()
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
                'SELECT id, operation_id, request_hash, state, preconditions_json, '
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
                'SELECT transaction_id, position, kind, path, before_hash, '
                'after_hash, parent_device, parent_inode, applied FROM "operation" '
                "LIMIT ?",
                (MAX_OPERATIONAL_ROWS + 1,),
            ).fetchall()
            corrupt = False
            if len(operation_rows) > MAX_OPERATIONAL_ROWS:
                details["codes"].append("transaction_operation_scan_truncated")
                details["deletion_codes"].append("transaction_state_unknown")
                operation_rows = operation_rows[:MAX_OPERATIONAL_ROWS]
            known_ids = {
                row["id"]
                for row in transaction_rows
                if isinstance(row["id"], str)
            }
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
                    code_row = database.execute(
                        'SELECT error_code FROM "transaction" WHERE id=?', (row["id"],)
                    ).fetchone() if "error_code" in transaction_columns else None
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
                valid_plan_hash = (
                    state == "preparing" and row["plan_hash"] == ""
                ) or (
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
                    and (
                        row["artifacts_pruned_at"] is None
                        or pruned is not None
                    )
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
                details["deletion_codes"].append(
                    "transaction_artifact_state_unknown"
                )
            for row in transaction_rows:
                transaction_id = row["id"]
                if not isinstance(transaction_id, str) or transaction_id not in known_ids:
                    corrupt = True
                    continue
                artifact_present = transaction_id in artifacts
                expects_artifacts = (
                    row["state"] != "discarded"
                    and row["artifacts_pruned_at"] is None
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
        with _readonly_database(path, state_root) as database:
            if _deadline_reached(deadline):
                raise TimeoutError("queue check deadline")
            tables = _tables(database, deadline)
            if "tasks" not in tables:
                raise sqlite3.DatabaseError("tasks table missing")
            task_columns = _columns(database, "tasks", deadline)
            if not {"state", "error_code", "blocked_capability"}.issubset(
                task_columns
            ):
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
                        row["result_sha256"]
                        if "result_sha256" in row_columns
                        else None
                    )
                if (
                    state == "leased"
                    and "lease_expires_at" in row_columns
                    and (_parse_utc(row["lease_expires_at"]) or datetime.min.replace(tzinfo=timezone.utc)) > now
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
    elif states["ready"] or states["leased"] or states["blocked"] or details["migration"] == "pending":
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


def _archive_check(
    root: Path, state_root: Path, deadline: float = float("inf")
) -> dict:
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
    if truncated or error or any(
        _safe_kind(item, state_root)[0] != "regular" for item in quarantine_entries
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
    problem = details["duplicates"] or details["quarantined"] or details["codes"] or details["index"] == "invalid"
    return _result(
        "archives",
        "error" if details["codes"] else "degraded" if problem else "ok",
        "Archive state requires operator attention." if problem else "Archive state is healthy.",
        details,
    )


def _claim_check(
    root: Path, state_root: Path, deadline: float = float("inf")
) -> dict:
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

        with _readonly_database(path, state_root) as database:
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
    status = "error" if details["index"] == "invalid" else "degraded" if details["diagnostics"] else "ok"
    return _result(
        "claims",
        status,
        "Claim index requires operator attention." if status != "ok" else "Claim index is healthy.",
        details,
    )


def _filesystem_check(
    state_root: Path, deadline: float = float("inf")
) -> dict:
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
        locking = reliable_memory._sqlite_lock_probe(
            state_root, deadline=probe_deadline
        )
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


def _append_blocker(blockers: list[dict[str, str]], code: str) -> None:
    if not any(item["code"] == code for item in blockers):
        blockers.append({"code": code})


def _run_deletion_check(
    state_root: Path,
    now: datetime,
    *,
    deadline: float = float("inf"),
    collected: dict[str, dict] | None = None,
) -> dict:
    """Return every reason the operational ``run/`` tree must be retained."""
    blockers: list[dict[str, str]] = []
    checks = dict(collected or {})
    if "transactions" not in checks:
        checks["transactions"] = _transaction_check(state_root, now, deadline)
    if "queue" not in checks:
        checks["queue"] = _queue_check(state_root, now, deadline)
    if "archives" not in checks:
        checks["archives"] = {
            "id": "archives",
            "status": "ok",
            "details": {"deletion_codes": []},
        }
    for check_id in ("transactions", "queue", "archives"):
        check = checks[check_id]
        details = check.get("details", {})
        for code in details.get("deletion_codes", []):
            _append_blocker(blockers, str(code))
        if details.get("read_error") and not details.get("deletion_codes"):
            _append_blocker(blockers, f"{check_id}_state_unreadable")
    if _deadline_reached(deadline):
        _append_blocker(blockers, "run_deletion_state_unknown")
    return {"allowed": not blockers, "blockers": blockers}


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
        )
        try:
            connection.set_progress_handler(
                lambda: int(time.monotonic() >= deadline), 1000
            )
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
            if manifest_error or any(
                not isinstance(item, str) for item in (manifest_paths or [])
            ):
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
            return _index_deferred(
                "FTS index check exceeded its time budget.", "budget_exhausted"
            )
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


def _scheduler_check(
    root: Path, state_root: Path, now: datetime, deadline: float
) -> dict:
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
        return _result("scheduler", "error", "Maintenance source or local state is invalid.", details)
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
    message = "MCP source is available; package capability was detected." if source else "MCP server source is missing."
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
        and "scripts/mcp_server.py" in args
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
    payload = "".join(
        json.dumps(item, separators=(",", ":")) + "\n" for item in requests
    ).encode("utf-8")
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
        expected = _expected_codex_runtime_hooks(
            root / "integrations" / "codex" / "hooks.json"
        )
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
            return False, f"runtime_hooks_{trust}" if trust in {"untrusted", "modified"} else "runtime_hooks_trust_unknown"
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


def _integration_check(
    root: Path, home: Path, *, deadline: float = float("inf")
) -> dict:
    sources = {
        "claude": root / "integrations" / "claude-code" / "settings.json",
        "opencode": root / "scripts" / "llm-wiki-memory-opencode.js",
        "codex": root / "integrations" / "codex" / "hooks.json",
        "cursor": root / "integrations" / "cursor" / "rules" / "llm-wiki.mdc",
        "antigravity": root / "integrations" / "antigravity" / "AGENTS.md",
    }
    host_configs = {
        "claude": (home / ".claude", [(home / ".claude" / "settings.json", ("LLM_WIKI_ROOT", "session_start_context.py"))]),
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
        "cursor": (home / ".cursor", [(home / ".cursor" / "rules" / "llm-wiki.mdc", ("LLM-Wiki", "LLM_WIKI_ROOT"))]),
        "antigravity": (
            home / ".gemini" / "antigravity",
            [(home / ".gemini" / "antigravity" / "AGENTS.md", ("LLM-Wiki", "LLM_WIKI_ROOT"))],
        ),
    }
    source_details = {name: path.is_file() for name, path in sources.items()}
    hosts = {}
    configured_missing = 0
    for name, (host_dir, configs) in host_configs.items():
        if not host_dir.exists():
            hosts[name] = {"status": "skipped", "message": "Optional host not installed."}
        elif name == "codex":
            hooks_active, reason = _codex_runtime_hooks_state(
                root, home, deadline=deadline
            )
            if hooks_active:
                hosts[name] = {
                    "status": "ok",
                    "message": "Official Codex hooks are active and trusted; review changes in /hooks.",
                    "capture_mode": "official-hooks",
                    "trust": "review-with-/hooks",
                }
            else:
                configured_missing += 1
                wrapper = _codex_wrapper_configured(root, home)
                hosts[name] = {
                    "status": "degraded",
                    "message": "Official Codex hooks are not verified; wrapper fallback is heartbeat-only."
                    if wrapper
                    else "Official Codex hooks are not verified and no capture fallback is configured.",
                    "reason": reason,
                    "capture_mode": "wrapper-fallback-heartbeat-only" if wrapper else "none",
                }
                if reason == "runtime_hooks_not_completed":
                    hosts[name]["not_completed"] = True
        elif name != "codex" and any(
            _contains_markers(path, markers) for path, markers in configs
        ):
            hosts[name] = {"status": "ok", "message": "User integration config detected."}
        elif name in {"cursor", "antigravity"}:
            hosts[name] = {
                "status": "skipped",
                "message": "Project-scoped integration is optional and was not checked.",
            }
        else:
            configured_missing += 1
            hosts[name] = {"status": "degraded", "message": "Host detected without LLM-Wiki config."}
    required_sources = {"claude", "opencode", "codex"}
    missing_sources = sum(
        not source_details[name] for name in required_sources
    )
    if missing_sources:
        status, message = "error", f"{missing_sources} integration source adapter(s) are missing."
    elif configured_missing:
        status, message = "degraded", f"{configured_missing} installed host(s) lack integration config."
    else:
        status, message = "ok", "Integration sources are available; optional hosts were checked."
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
            acquired = datetime.fromisoformat(
                str(existing.get("lock_acquired_at", ""))
            )
            if acquired.tzinfo is None:
                acquired = acquired.replace(tzinfo=timezone.utc)
            stale = (
                now - acquired.astimezone(timezone.utc)
            ).total_seconds() > LOCK_STALE_SECONDS
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
            if stat.S_ISLNK(component_info.st_mode) or not stat.S_ISDIR(
                component_info.st_mode
            ):
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
            database.execute(
                f"ALTER TABLE maintenance_owners ADD COLUMN {name} {declaration}"
            )


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


def _require_maintenance_owner(
    coordinator: Any, lease: dict[str, object]
) -> None:
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


def _release_maintenance_owner(
    coordinator: Any, lease: dict[str, object]
) -> None:
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
    repair_actions: set[str] | frozenset[str] | None = None,
    now: datetime | None = None,
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
) -> dict:
    """Return a JSON-safe local health report; mutate only with ``repair=True``."""
    selected_repairs = set(VALID_REPAIR_ACTIONS if repair_actions is None else repair_actions)
    unknown_repairs = selected_repairs - VALID_REPAIR_ACTIONS
    if unknown_repairs:
        raise ValueError(f"unknown doctor repair actions: {sorted(unknown_repairs)}")
    root_path = Path(root or os.environ.get("LLM_WIKI_ROOT", Path(__file__).resolve().parent.parent)).resolve()
    state_path = Path(
        os.path.abspath(state_root or os.environ.get("LLM_WIKI_STATE_ROOT", root_path))
    )
    home_path = Path(home).resolve() if home is not None else Path.home().resolve()
    generated_at = _as_utc(now)
    repaired: list[dict] = []
    repair_errors: dict[str, list[str]] = {}
    repair_deferred: set[str] = set()
    deadline = time.monotonic() + max(0.0, time_budget_seconds)

    if repair:
        maintenance: tuple[Any, dict[str, object]] | None = None
        guard_entered = False
        try:
            maintenance = _acquire_maintenance_owner(
                root_path, state_path, generated_at
            )
            if maintenance is None:
                deferred_by_action = {
                    "runtime": {"runtime"},
                    "transactions": {"transactions"},
                    "queue": {"queue"},
                    "indexes": {"index", "claims"},
                    "archives": {"archives"},
                }
                for action in selected_repairs:
                    repair_deferred.update(deferred_by_action[action])
            else:
                coordinator, lease = maintenance
                with _MaintenanceHeartbeat(
                    coordinator, lease, deadline=deadline
                ) as guard:
                    guard_entered = True
                    if "runtime" in selected_repairs:
                        guard.run(_repair_runtime, state_path, repaired)
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
                            not marker_existed
                            or migration.imported
                            or migration.quarantined
                        ):
                            repaired.append(
                                {
                                    "action": "migrate_queue",
                                    "count": migration.imported
                                    + migration.quarantined,
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
                                            if index_after["details"].get("freshness")
                                            == "missing"
                                            else "Index repair failed: rebuilt index did not validate as fresh"
                                        )
                                        repair_errors.setdefault("index", []).append(
                                            message
                                        )
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
                        archive_before = _archive_check(
                            root_path, state_path, deadline
                        )
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
                            repaired.append(
                                {"action": "run_bounded_worker", "count": processed}
                            )
        except Exception as exc:  # noqa: BLE001
            repair_errors.setdefault("runtime", []).append(
                f"Repair failed: {type(exc).__name__}"
            )
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
            "index",
            lambda: _index_check(
                state_path, generated_at, deadline, root=root_path
            ),
        ),
        (
            "scheduler",
            lambda: _scheduler_check(
                root_path, state_path, generated_at, deadline
            ),
        ),
        ("mcp", lambda: _mcp_check(root_path)),
        (
            "integrations",
            lambda: _integration_check(root_path, home_path, deadline=deadline),
        ),
    )
    for check_id, operation in remaining:
        if time.monotonic() >= deadline:
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
    counts = {status: sum(check["status"] == status for check in checks) for status in VALID_STATUSES}
    overall = "error" if counts["error"] else "degraded" if counts["degraded"] else "ok"
    collected = {check["id"]: check for check in checks}
    run_deletion = _run_deletion_check(
        state_path,
        generated_at,
        deadline=deadline,
        collected=collected,
    )
    checks.append(
        _result(
            "run_deletion",
            "ok",
            "Runtime history may be deleted." if run_deletion["allowed"] else "Runtime history must be retained.",
            run_deletion,
        )
    )
    counts = {status: sum(check["status"] == status for check in checks) for status in VALID_STATUSES}
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
            entries.append(f"{check.get('id', 'unknown')} ({check['status']}): {check.get('message', '')}")
    text = "; ".join(entries)
    if len(text) <= SUMMARY_LIMIT:
        return text
    return text[: SUMMARY_LIMIT - 3].rstrip() + "..."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check local LLM-Wiki health.")
    parser.add_argument("--repair", action="store_true", help="Apply safe idempotent repairs.")
    parser.add_argument("--json", action="store_true", help="Emit the structured report as JSON.")
    args = parser.parse_args(argv)
    report = run_doctor(repair=args.repair)
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
