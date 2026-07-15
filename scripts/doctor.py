"""Agent-readable local health checks and conservative repairs."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
INDEX_FRESH_SECONDS = 24 * 60 * 60
STALE_LEASE_SECONDS = 10 * 60
PERMANENT_FAILURE_ATTEMPTS = 5
SUMMARY_LIMIT = 600
VALID_STATUSES = ("ok", "degraded", "error", "skipped")
RUNTIME_DIRECTORIES = ("run", "logs", "cache")
MAX_QUEUE_FILES = 200
MAX_QUEUE_FILE_BYTES = 64 * 1024
MAX_CONFIG_BYTES = 64 * 1024
MAX_STATE_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_INDEX_PATHS = 10_000
MAX_LOCK_BYTES = 4096
MAX_QUEUE_RESULT_BYTES = 8 * 1024 * 1024
LOCK_STALE_SECONDS = 10 * 60
DEFAULT_TIME_BUDGET_SECONDS = 5.0
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
    kind, info = _safe_kind(path, root)
    if kind != "regular" or info is None:
        return None, "unsafe"
    if info.st_size > max_bytes:
        return None, "oversized"
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                return None, "unsafe"
            raw = os.read(fd, max_bytes + 1)
        finally:
            os.close(fd)
        if time.monotonic() >= deadline:
            return None, "budget"
        if len(raw) > max_bytes:
            return None, "oversized"
        value = json.loads(raw.decode("utf-8"))
        return (value, None) if isinstance(value, expected_type) else (None, "invalid")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
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


def _queue_check(state_root: Path, now: datetime, deadline: float) -> dict:
    database_path = state_root / "run" / "queue.sqlite3"
    if _safe_kind(database_path, state_root)[0] == "regular":
        return _queue_v2_check(state_root, now, deadline)
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
    }
    if permanently_failed:
        status, message = "error", f"Queue has {permanently_failed} permanently failed task(s)."
    elif pending or stale_leases or unsafe_entries or oversized_entries or truncated:
        status, message = "degraded", f"Queue has {pending} pending task(s) and {stale_leases} stale lease(s)."
    else:
        status, message = "ok", "Queue has no pending or stale work."
    return _result("queue", status, message, details)


def _readonly_database(path: Path) -> sqlite3.Connection:
    database = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0
    )
    database.row_factory = sqlite3.Row
    return database


def _tables(database: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _columns(database: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in database.execute(f'PRAGMA table_info("{table}")')}


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
    if not isinstance(pid, int) or not _pid_alive(pid):
        return False
    expiry = _parse_utc(row["expires_at"]) if "expires_at" in columns else None
    return expiry is None or expiry > now


def _transaction_check(state_root: Path, now: datetime) -> dict:
    path = state_root / "run" / "markdown-transactions.sqlite3"
    states = {state: 0 for state in TRANSACTION_STATES}
    details: dict[str, Any] = {
        "states": states,
        "codes": [],
        "undo_retained": 0,
        "live_project_leases": 0,
        "live_writers": 0,
        "live_maintenance_owners": 0,
    }
    kind, _ = _safe_kind(path, state_root)
    if kind == "missing":
        return _result("transactions", "ok", "No transaction database exists.", details)
    if kind != "regular":
        return _result("transactions", "error", "Transaction database is unsafe.", details)
    try:
        with _readonly_database(path) as database:
            tables = _tables(database)
            if "transaction" not in tables:
                raise sqlite3.DatabaseError("transaction table missing")
            transaction_columns = _columns(database, "transaction")
            transaction_query = (
                'SELECT id, state, updated_at, artifacts_pruned_at FROM "transaction"'
                if "artifacts_pruned_at" in transaction_columns
                else 'SELECT id, state, updated_at, NULL AS artifacts_pruned_at FROM "transaction"'
            )
            rows = database.execute(transaction_query + " LIMIT 10001").fetchall()
            if len(rows) > 10_000:
                details["codes"].append("transaction_scan_truncated")
                rows = rows[:10_000]
            codes: set[str] = set()
            cutoff = now - timedelta(days=UNDO_RETENTION_DAYS)
            for row in rows:
                state = str(row["state"])
                states[state] = states.get(state, 0) + 1
                if state in {"conflicted", "quarantined"}:
                    code_row = database.execute(
                        'SELECT error_code FROM "transaction" WHERE id=?', (row["id"],)
                    ).fetchone() if "error_code" in transaction_columns else None
                    if code_row is not None and code_row[0]:
                        codes.add(str(code_row[0]))
                updated = _parse_utc(row["updated_at"])
                artifact = state_root / "run" / "transactions" / str(row["id"])
                if (
                    state == "committed"
                    and updated is not None
                    and updated >= cutoff
                    and row["artifacts_pruned_at"] is None
                    and artifact.is_dir()
                ):
                    details["undo_retained"] += 1
            details["codes"] = sorted(set(details["codes"]) | codes)
            if "project_leases" in tables:
                details["live_project_leases"] = sum(
                    (_parse_utc(row[0]) or datetime.min.replace(tzinfo=timezone.utc)) > now
                    for row in database.execute("SELECT expires_at FROM project_leases")
                )
            if "writer_owners" in tables:
                details["live_writers"] = sum(
                    _live_owner(row, now, pid_column="process_id")
                    for row in database.execute("SELECT * FROM writer_owners")
                )
            if "maintenance_owners" in tables:
                details["live_maintenance_owners"] = sum(
                    _live_owner(row, now, pid_column="process_id")
                    for row in database.execute("SELECT * FROM maintenance_owners")
                )
    except (OSError, sqlite3.Error, ValueError):
        return _result("transactions", "error", "Transaction state is unreadable.", details)
    problem = (
        sum(states[state] for state in ("preparing", "prepared", "applying"))
        + states["conflicted"]
        + states["quarantined"]
    )
    status = "error" if states["conflicted"] or states["quarantined"] else "degraded" if problem else "ok"
    return _result(
        "transactions",
        status,
        "Transaction state requires operator attention." if problem else "Transaction state is healthy.",
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
        "migration": "not-started",
    }
    marker = state_root / "run" / "queue-migrated-v2"
    legacy = state_root / "run" / "queue"
    details["migration"] = "complete" if marker.is_file() else "pending"
    if marker.exists() and legacy.is_dir() and any(legacy.iterdir()):
        details["migration"] = "conflict"
    try:
        with _readonly_database(path) as database:
            tables = _tables(database)
            if "tasks" not in tables:
                raise sqlite3.DatabaseError("tasks table missing")
            rows = database.execute("SELECT * FROM tasks LIMIT 10001").fetchall()
            if len(rows) > 10_000:
                details["codes"].append("queue_scan_truncated")
                rows = rows[:10_000]
            codes: set[str] = set()
            capabilities: set[str] = set()
            references: set[str] = set()
            for row in rows:
                state = str(row["state"])
                states[state] = states.get(state, 0) + 1
                row_columns = set(row.keys())
                if "error_code" in row_columns and row["error_code"]:
                    codes.add(str(row["error_code"]))
                if "blocked_capability" in row_columns and row["blocked_capability"]:
                    capabilities.add(str(row["blocked_capability"]))
                if "result_reference" in row_columns and row["result_reference"]:
                    references.add(str(row["result_reference"]))
                if (
                    state == "leased"
                    and "lease_expires_at" in row_columns
                    and (_parse_utc(row["lease_expires_at"]) or datetime.min.replace(tzinfo=timezone.utc)) > now
                ):
                    details["live_workers"] += 1
            details["codes"] = sorted(set(details["codes"]) | codes)
            details["capabilities"] = sorted(capabilities)
            if "source_failures" in tables:
                details["source_failures"] = int(
                    database.execute("SELECT count(*) FROM source_failures").fetchone()[0]
                )
            if "queue_ownership" in tables:
                for row in database.execute("SELECT * FROM queue_ownership"):
                    if row["token"] is None or not _live_owner(row, now, pid_column="pid"):
                        continue
                    if row["role"] == "worker":
                        details["live_workers"] += 1
                    if row["role"] == "migration":
                        details["live_migrations"] += 1
            results = state_root / "run" / "queue-results"
            files = list(results.glob("*.result")) if results.is_dir() else []
            details["results_retained"] = len(files)
            for reference in references:
                candidate = state_root / reference
                if not candidate.is_file():
                    details["results_invalid"] += 1
                    continue
                matching = next(
                    (row for row in rows if "result_reference" in row.keys() and row["result_reference"] == reference),
                    None,
                )
                expected = (
                    matching["result_sha256"]
                    if matching is not None and "result_sha256" in matching.keys()
                    else None
                )
                try:
                    raw = (
                        candidate.read_bytes()
                        if candidate.stat().st_size <= MAX_QUEUE_RESULT_BYTES
                        else b""
                    )
                except OSError:
                    raw = b""
                if not isinstance(expected, str) or hashlib.sha256(raw).hexdigest() != expected:
                    details["results_invalid"] += 1
    except (OSError, sqlite3.Error, ValueError):
        return _result("queue", "error", "Queue state is unreadable.", details)
    if time.monotonic() >= deadline:
        details["budget_exhausted"] = True
    if states["dead"] or details["results_invalid"] or details["migration"] == "conflict":
        status = "error"
    elif states["ready"] or states["leased"] or states["blocked"] or details["migration"] == "pending":
        status = "degraded"
    else:
        status = "ok"
    return _result(
        "queue",
        status,
        "Queue state requires operator attention." if status != "ok" else "Queue state is healthy.",
        details,
    )


def _archive_check(root: Path, state_root: Path) -> dict:
    archive = root / "knowledge" / "daily" / "archive"
    quarantine = state_root / "run" / "archive-quarantine"
    details = {"bags": 0, "duplicates": 0, "quarantined": 0, "index": "missing", "codes": []}
    if quarantine.is_dir():
        details["quarantined"] = sum(1 for item in quarantine.rglob("*") if item.is_file())
    if not archive.exists():
        return _result("archives", "ok", "No archive exists.", details)
    try:
        months = [
            month
            for month in list(archive.iterdir())[:121]
            if month.is_dir() and re.fullmatch(r"\d{4}-\d{2}", month.name)
        ]
        if len(months) > 120:
            details["codes"].append("archive_scan_truncated")
            months = months[:120]
        bags = []
        for month in months:
            for item in list(month.iterdir())[:10_001]:
                if item.is_dir() and item.name.startswith("bag-"):
                    bags.append(item)
                    if len(bags) > 10_000:
                        details["codes"].append("archive_scan_truncated")
                        bags = bags[:10_000]
                        break
            if len(bags) >= 10_000:
                break
        details["bags"] = len(bags)
        seen: set[tuple[object, object]] = set()
        bag_paths: set[str] = set()
        for bag in bags:
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
        index, problem = _read_bounded_json(
            archive / "archive-index.json", archive, max_bytes=MAX_MANIFEST_BYTES
        ) if (archive / "archive-index.json").exists() else (None, "missing")
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
    except OSError:
        details["codes"].append("archive_unreadable")
    problem = details["duplicates"] or details["quarantined"] or details["codes"] or details["index"] == "invalid"
    return _result(
        "archives",
        "error" if details["codes"] else "degraded" if problem else "ok",
        "Archive state requires operator attention." if problem else "Archive state is healthy.",
        details,
    )


def _claim_check(root: Path, state_root: Path) -> dict:
    path = state_root / "cache" / "claims.sqlite3"
    details = {"index": "missing", "claims": 0, "diagnostics": 0, "codes": []}
    if not path.exists():
        return _result("claims", "degraded", "Claim index is missing.", details)
    try:
        from claims import ClaimIndex

        with _readonly_database(path) as database:
            compatible = ClaimIndex._schema_compatible(database)
            details["index"] = "valid" if compatible else "invalid"
            if compatible:
                details["claims"] = int(database.execute("SELECT count(*) FROM claim").fetchone()[0])
                rows = database.execute(
                    "SELECT code,count(*) FROM claim_index_diagnostic GROUP BY code ORDER BY code"
                ).fetchall()
                details["diagnostics"] = sum(int(row[1]) for row in rows)
                details["codes"] = [str(row[0]) for row in rows]
    except (OSError, sqlite3.Error, ValueError):
        details["index"] = "invalid"
    status = "error" if details["index"] == "invalid" else "degraded" if details["diagnostics"] else "ok"
    return _result(
        "claims",
        status,
        "Claim index requires operator attention." if status != "ok" else "Claim index is healthy.",
        details,
    )


def _filesystem_check(state_root: Path) -> dict:
    raw = str(state_root)
    local = not raw.startswith(("\\\\", "//"))
    details = {"local": local, "locking": "supported" if local else "unsupported"}
    return _result(
        "filesystem",
        "ok" if local else "error",
        "Runtime filesystem supports local locking." if local else "Runtime must use a local filesystem.",
        details,
    )


def _append_blocker(blockers: list[dict[str, str]], code: str) -> None:
    if not any(item["code"] == code for item in blockers):
        blockers.append({"code": code})


def _run_deletion_check(state_root: Path, now: datetime) -> dict:
    """Return every reason the operational ``run/`` tree must be retained."""
    blockers: list[dict[str, str]] = []
    transaction_check = _transaction_check(state_root, now)
    transaction = transaction_check["details"]
    if transaction_check["status"] == "error" and not any(
        transaction["states"].values()
    ):
        _append_blocker(blockers, "transaction_state_unreadable")
    states = transaction["states"]
    if any(states.get(state, 0) for state in ("preparing", "prepared", "applying")):
        _append_blocker(blockers, "transaction_nonterminal")
    if states.get("conflicted", 0):
        _append_blocker(blockers, "transaction_conflicted")
    if states.get("quarantined", 0):
        _append_blocker(blockers, "transaction_quarantined")
    if transaction["undo_retained"]:
        _append_blocker(blockers, "transaction_undo_retained")
    if transaction["live_project_leases"]:
        _append_blocker(blockers, "project_lease_live")
    if transaction["live_writers"]:
        _append_blocker(blockers, "writer_live")
    if transaction["live_maintenance_owners"]:
        _append_blocker(blockers, "maintenance_owner_live")

    queue_path = state_root / "run" / "queue.sqlite3"
    if queue_path.is_file():
        try:
            queue_check = _queue_v2_check(state_root, now, float("inf"))
            queue = queue_check["details"]
            if queue_check["status"] == "error" and not sum(queue["states"].values()):
                _append_blocker(blockers, "queue_state_unreadable")
            if sum(queue["states"].values()):
                _append_blocker(blockers, "queue_task_retained")
            if queue["results_retained"]:
                _append_blocker(blockers, "queue_result_retained")
            if queue["live_workers"]:
                _append_blocker(blockers, "queue_worker_live")
            if queue["live_migrations"]:
                _append_blocker(blockers, "queue_migration_live")
        except (OSError, sqlite3.Error, ValueError):
            _append_blocker(blockers, "queue_state_unreadable")
    else:
        results = state_root / "run" / "queue-results"
        if results.is_dir() and any(results.iterdir()):
            _append_blocker(blockers, "queue_result_retained")
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
        connection = sqlite3.connect(
            f"{index.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=0,
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
            if manifest_error or any(not isinstance(item, str) for item in manifest_paths):
                raise ValueError("invalid manifest")
            if sorted(manifest_paths) != sorted(indexed_paths):
                raise ValueError("manifest mismatch")
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
    age = max(0, int((now - timestamp).total_seconds()))
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


def _integration_check(root: Path, home: Path) -> dict:
    sources = {
        "claude": root / "integrations" / "claude-code" / "settings.json",
        "opencode": root / "scripts" / "llm-wiki-memory-opencode.js",
        "codex": root / "scripts" / "codex_memory.py",
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
        "codex": (home / ".codex", [(home / ".codex" / "config.toml", ("codex-memory-wrapper", "codex_memory"))]),
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
        elif any(_contains_markers(path, markers) for path, markers in configs):
            hosts[name] = {"status": "ok", "message": "User integration config detected."}
        else:
            configured_missing += 1
            hosts[name] = {"status": "degraded", "message": "Host detected without LLM-Wiki config."}
    missing_sources = sum(not present for present in source_details.values())
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


def _rebuild_index(root: Path, state_root: Path) -> None:
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
            kind, _ = _safe_kind(page, notes)
            if kind == "regular":
                pages.append(page)
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


def _release_maintenance_owner(
    coordinator: Any, lease: dict[str, object]
) -> None:
    with coordinator._connect() as database:
        database.execute("BEGIN IMMEDIATE")
        database.execute(
            """UPDATE maintenance_owners
               SET owner_token='',process_id=0,heartbeat_at=?,expires_at=?
               WHERE owner_name='doctor' AND owner_token=? AND fencing_epoch=?""",
            (
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
                lease["token"],
                lease["epoch"],
            ),
        )
        database.commit()


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
    with sqlite3.connect(path) as database:
        changed = database.execute(
            f"UPDATE tasks SET state='ready',blocked_capability=NULL,error_code=NULL "
            f"WHERE state='blocked' AND blocked_capability IN ({placeholders})",
            sorted(repaired),
        ).rowcount
        database.commit()
    return changed


def _run_bounded_worker(state_root: Path) -> int:
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
    owner = _acquire_queue_owner(
        state_root, "worker", "worker_busy", ttl_seconds=MAINTENANCE_LEASE_SECONDS
    )
    try:
        summary = run_worker(
            _manual_processor,
            max_tasks=20,
            max_seconds=1,
            idle_seconds=0,
        )
        return summary.processed
    finally:
        _release_queue_owner(owner)


def run_doctor(
    root: Path | str | None = None,
    state_root: Path | str | None = None,
    home: Path | str | None = None,
    repair: bool = False,
    now: datetime | None = None,
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
) -> dict:
    """Return a JSON-safe local health report; mutate only with ``repair=True``."""
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
        try:
            maintenance = _acquire_maintenance_owner(
                root_path, state_path, generated_at
            )
            if maintenance is None:
                repair_deferred.update(
                    {"runtime", "transactions", "queue", "index", "archives", "claims"}
                )
            else:
                coordinator, lease = maintenance
                _repair_runtime(state_path, repaired)
                _heartbeat_maintenance_owner(coordinator, lease)
                recovered = coordinator.recover(writer_wait_seconds=0)
                if recovered:
                    repaired.append(
                        {"action": "recover_transactions", "count": len(recovered)}
                    )
                _heartbeat_maintenance_owner(coordinator, lease)
                legacy_available = _repair_leases(
                    state_path, generated_at, repaired
                )
                if not legacy_available:
                    repair_deferred.add("queue")
                from memory_queue import MemoryQueue, migrate_legacy_queue

                marker = state_path / "run" / "queue-migrated-v2"
                marker_existed = marker.exists()
                migration = (
                    migrate_legacy_queue(state_path)
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
                            "count": migration.imported + migration.quarantined,
                        }
                    )
                if migration is not None or marker.is_file():
                    MemoryQueue(state_path)
                queue_v2_ready = migration is not None or marker.is_file()
                _heartbeat_maintenance_owner(coordinator, lease)
                index_before = _index_check(state_path, generated_at, deadline)
                if (
                    index_before["details"].get("repairable")
                    and index_before["status"] != "ok"
                ):
                    index_lock = state_path / "cache" / ".doctor-index.lock"
                    lock_token = _acquire_lock(
                        index_lock, state_path / "cache", generated_at
                    )
                    if lock_token is None:
                        repair_deferred.add("index")
                    else:
                        try:
                            _rebuild_index(root_path, state_path)
                            index_after = _index_check(
                                state_path, generated_at, deadline
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
                                repair_errors.setdefault("index", []).append(message)
                        except Exception as exc:  # noqa: BLE001
                            repair_errors.setdefault("index", []).append(
                                f"Index repair failed: {type(exc).__name__}"
                            )
                        finally:
                            _release_lock(
                                index_lock, state_path / "cache", lock_token
                            )
                _heartbeat_maintenance_owner(coordinator, lease)
                archive_before = _archive_check(root_path, state_path)
                archive_root = root_path / "knowledge" / "daily" / "archive"
                if archive_root.exists() and archive_before["status"] != "ok":
                    from archive_daily import DailyArchiver

                    DailyArchiver(root_path, state_path).recover()
                    repaired.append({"action": "recover_archives"})
                claim_before = _claim_check(root_path, state_path)
                if claim_before["status"] != "ok":
                    from claims import ClaimIndex

                    sources = [root_path / "knowledge" / "notes"]
                    projects = root_path / "knowledge" / "projects"
                    if projects.is_dir():
                        sources.append(projects)
                    ClaimIndex(state_path, vault=root_path).rebuild(sources)
                    repaired.append({"action": "rebuild_claim_index"})
                _heartbeat_maintenance_owner(coordinator, lease)
                unblocked = (
                    _repair_queue_capabilities(state_path) if queue_v2_ready else 0
                )
                if unblocked:
                    repaired.append(
                        {"action": "unblock_capabilities", "count": unblocked}
                    )
                processed = _run_bounded_worker(state_path) if queue_v2_ready else 0
                if processed:
                    repaired.append({"action": "run_bounded_worker", "count": processed})
        except Exception as exc:  # noqa: BLE001
            repair_errors.setdefault("runtime", []).append(
                f"Repair failed: {type(exc).__name__}"
            )
        finally:
            if maintenance is not None:
                try:
                    _release_maintenance_owner(*maintenance)
                except Exception as exc:  # noqa: BLE001
                    repair_errors.setdefault("runtime", []).append(
                        f"Maintenance owner release failed: {type(exc).__name__}"
                    )

    checks = [
        _environment_check(root_path, state_path),
        _runtime_check(state_path),
        _filesystem_check(state_path),
        _transaction_check(state_path, generated_at),
        _queue_check(state_path, generated_at, deadline),
        _archive_check(root_path, state_path),
        _claim_check(root_path, state_path),
    ]
    remaining = (
        ("index", lambda: _index_check(state_path, generated_at, deadline)),
        (
            "scheduler",
            lambda: _scheduler_check(
                root_path, state_path, generated_at, deadline
            ),
        ),
        ("mcp", lambda: _mcp_check(root_path)),
        ("integrations", lambda: _integration_check(root_path, home_path)),
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
    run_deletion = _run_deletion_check(state_path, generated_at)
    checks.append(
        _result(
            "run_deletion",
            "ok" if run_deletion["allowed"] else "degraded",
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
