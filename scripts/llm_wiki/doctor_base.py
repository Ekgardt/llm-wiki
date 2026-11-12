"""Doctor's shared findings vocabulary, bounded reads and runtime inventory."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import stat
import sys
import time
from pathlib import Path
from typing import Any

import generation_maintenance  # noqa: E402
import integration_hook_config as _hook_config
from install_state import (
    validate_install_state,
)
from llm_wiki import monotonic_deadline  # noqa: E402
from python_series import SUPPORTED_SERIES
from reliable_memory import (
    open_readonly_operational_db,
    read_runtime_bytes,
)

SCHEMA_VERSION = "1.0"


# The degraded-health summary injected into a session's context: past it the text is
# cut and ends in "..." so the reader sees the cut. Basis unknown: value predates
# measurement; review when a summary a session needs is seen cut.
SUMMARY_LIMIT = 600
VALID_STATUSES = ("ok", "degraded", "error", "skipped")


RUNTIME_DIRECTORIES = ("run", "logs", "cache")


# `read_bounded_json`'s default bound. Every doctor caller names its own bound, so
# this applies only to a caller that does not; 64 KiB is refused, never cut.
MAX_QUEUE_FILE_BYTES = 64 * 1024


# The hook configuration is read under the bound the installer writes it with.
MAX_CONFIG_BYTES = _hook_config.MAX_CONFIG_BYTES


# An archive bag's manifest or index, read to check it (refused past the bound, not
# cut). Basis unknown: value predates measurement, and the installed vault held no
# archive bag on 2026-09-27 to measure; review when the first bags are written.
MAX_MANIFEST_BYTES = 256 * 1024


# A runtime lock file doctor reads (state and index locks); installer locks are bounded at 1 KiB.
MAX_LOCK_BYTES = 4096


# SQLite opens page data on demand. Health scans use their existing deadline and
# streamed rows; a whole-file byte ceiling is not a query resource budget.
# Rows one health read takes from a small operational table (owners, leases, queue
# ownership, queue and archive-index rows). A table past it is reported with its
# `*_unknown`/`*_truncated` code and refuses `run/` deletion; nothing is judged from
# rows unseen. The transaction and operation tables no longer read under it: they are
# streamed whole (docs/research/2026-09-27-doctor-reads-every-transaction.md), because
# at 29 275 rows the installed vault outgrew it. Basis unknown for the rest: value
# predates measurement; review when any of those tables nears it.
MAX_OPERATIONAL_ROWS = 10_000


# A commit on a rollback-journal database locks readers out for milliseconds.
# Wait that out rather than reporting a healthy database as unreadable, but
# stay far below the default time budget above.
READ_BUSY_MS = 250


INDEX_COLUMNS = {"path", "title", "summary", "body", "project", "timestamp", "slug"}
TRANSACTION_STATES = (
    "preparing",
    "prepared",
    "applying",
    "committed",
    "discarded",
    "conflicted",
    "quarantined",
    # An operator discard rolls back through `aborting` to `aborted`; doctor
    # called both "a state this runtime does not define" (audit 2026-09-26 C-12).
    "aborting",
    "aborted",
)


QUEUE_STATES = ("ready", "leased", "blocked", "succeeded", "dead", "cancelled")


def environment_check(root: Path, state_root: Path) -> dict:
    python_ok = tuple(sys.version_info[:2]) == SUPPORTED_SERIES
    root_ok = root.is_dir()
    state_parent_ok = state_root.is_dir()
    layout = {
        "knowledge": (root / "knowledge").is_dir(),
        "knowledge_notes": (root / "knowledge" / "notes").is_dir(),
        "scripts": (root / "scripts").is_dir(),
    }
    status = _environment_status(python_ok, root_ok, state_parent_ok, layout)
    details = {
        "python": {
            "status": ok_or_error(python_ok),
            "version": ".".join(str(part) for part in sys.version_info[:3]),
        },
        "vault_root": {"status": ok_or_error(root_ok)},
        "state_root": {"status": ok_or_error(state_parent_ok)},
        "layout": layout,
        "provider": _provider_setting(),
    }
    return _with_provider_finding(
        generation_maintenance.result("environment", status, _environment_message(status), details)
    )


def _provider_setting() -> dict[str, object]:
    """`MEMORY_LLM_PROVIDER` as the calls read it, and whether it names a provider.

    A name that is no provider yields no provider at all (audit C-3); saying so
    here is the only way the operator learns of the typo.
    """
    from llm_client import KNOWN_PROVIDERS, forced_provider, local_only_conflict

    value = forced_provider()
    known = value in {"", "fake", *KNOWN_PROVIDERS}
    return {
        "value": value or "auto",
        "known": known,
        "accepted": ["auto (unset)", *KNOWN_PROVIDERS],
        "local_only_conflict": local_only_conflict(),
    }


def _with_provider_finding(result: dict) -> dict:
    return _with_local_only_finding(_with_unknown_provider_finding(result))


def _with_local_only_finding(result: dict) -> dict:
    conflict = result["details"]["provider"]["local_only_conflict"]
    if conflict is None or result["status"] != "ok":
        return result
    result["status"] = "degraded"
    result["message"] = (
        f"OLLAMA_NO_CLOUD=1 refuses the saved model {conflict}, so no model is called; "
        "run `choose_model.py set ollama/<model>` to stay local, or unset OLLAMA_NO_CLOUD."
    )
    return result


def _with_unknown_provider_finding(result: dict) -> dict:
    provider = result["details"]["provider"]
    if provider["known"] or result["status"] != "ok":
        return result
    result["status"] = "degraded"
    result["message"] = (
        f"MEMORY_LLM_PROVIDER={provider['value']} names no provider, so no model is called; "
        f"use one of: {', '.join(provider['accepted'])}."
    )
    return result


def ok_or_error(value: bool) -> str:
    return "ok" if value else "error"


def _environment_status(
    python_ok: bool, root_ok: bool, state_parent_ok: bool, layout: dict
) -> str:
    roots_ok = root_ok and state_parent_ok
    return ok_or_error(python_ok and roots_ok and all(layout.values()))


def _environment_message(status: str) -> str:
    if status == "ok":
        return "Configured roots and source layout are available."
    return "Configured environment is incomplete."


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


def _runtime_directory_state(state_root: Path) -> tuple[dict[str, object], tuple[int, int, int]]:
    details: dict[str, object] = {}
    counts = [0, 0, 0]
    for relative in RUNTIME_DIRECTORIES:
        path = state_root / relative
        kind, _ = generation_maintenance.safe_kind(path, state_root)
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
    ordered = (
        (unsafe, ("error", "Runtime paths include unsafe entries.")),
        (unwritable, ("error", "Runtime directories are not writable.")),
        (missing, ("degraded", f"{missing} runtime directories are missing.")),
    )
    for flagged, verdict in ordered:
        if flagged:
            return verdict
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


def runtime_check(state_root: Path) -> dict:
    details, counts = _runtime_directory_state(state_root)
    status, message = _runtime_directory_result(counts)
    install = validate_install_state(state_root)
    status, message = _merge_install_health(status, message, details, install)
    return generation_maintenance.result("runtime", status, message, details)


def _bounded_json_entry_problem(path: Path, root: Path, deadline: float) -> str | None:
    """Why the path cannot be opened at all, before its size is consulted."""
    if time.monotonic() >= deadline:
        return "budget"
    if generation_maintenance.safe_kind(path, root)[0] != "regular":
        return "unsafe"
    return None


def _bounded_json_path_problem(
    path: Path,
    root: Path,
    max_bytes: int,
    deadline: float,
) -> str | None:
    entry = _bounded_json_entry_problem(path, root, deadline)
    if entry is not None:
        return entry
    try:
        oversized = path.lstat().st_size > max_bytes
    except OSError:
        return "invalid"
    if oversized:
        return "oversized"
    return None


def read_bounded_json(
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


def read_busy_ms(deadline: float | None) -> int:
    """Wait out a brief commit lock, keeping budget left to report what happened.

    Spending the whole remaining budget on the wait would turn every busy
    database into an indistinguishable "budget exhausted" verdict.
    """
    if deadline is None or not math.isfinite(deadline):
        return READ_BUSY_MS
    remaining_ms = (deadline - time.monotonic()) * 1000
    return max(0, min(READ_BUSY_MS, int(remaining_ms / 2)))


def readonly_database(
    path: Path,
    state_root: Path,
    *,
    max_bytes: int | None = None,
    deadline: float | None = None,
) -> sqlite3.Connection:
    return open_readonly_operational_db(
        path,
        state_root,
        max_bytes=max_bytes,
        owner_only=False,
        busy_ms=read_busy_ms(deadline),
        deadline=deadline,
    )


def database_tables(database: sqlite3.Connection, deadline: float = float("inf")) -> set[str]:
    if monotonic_deadline.deadline_reached(deadline):
        raise TimeoutError("database schema deadline")
    rows = database.execute(
        "SELECT name FROM sqlite_master WHERE type='table' LIMIT 257"
    ).fetchall()
    if len(rows) > 256:
        raise sqlite3.DatabaseError("database schema exceeds table limit")
    return {str(row[0]) for row in rows}


def owner_row_known(row: sqlite3.Row, *, pid_column: str) -> bool:
    columns = set(row.keys())
    pid = row[pid_column] if pid_column in columns else None
    expiry = generation_maintenance.parse_utc(row["expires_at"]) if "expires_at" in columns else None
    return isinstance(pid, int) and pid > 0 or expiry is not None


def archive_path(root: Path) -> Path:
    return root / "knowledge" / "daily" / "archive"


def database_sidecar_present(path: Path, state_root: Path) -> bool:
    return any(
        generation_maintenance.safe_kind(Path(f"{path}{suffix}"), state_root)[0] != "missing"
        for suffix in ("-journal", "-wal", "-shm")
    )
