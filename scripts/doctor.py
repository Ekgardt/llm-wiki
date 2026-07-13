"""Agent-readable local health checks and conservative repairs."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
import sqlite3
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
INDEX_FRESH_SECONDS = 24 * 60 * 60
STALE_LEASE_SECONDS = 10 * 60
PERMANENT_FAILURE_ATTEMPTS = 5
SUMMARY_LIMIT = 600
VALID_STATUSES = ("ok", "degraded", "error", "skipped")
RUNTIME_DIRECTORIES = ("run", "run/queue", "logs", "cache")
MAX_QUEUE_FILES = 200
MAX_QUEUE_FILE_BYTES = 64 * 1024
MAX_CONFIG_BYTES = 64 * 1024
MAX_STATE_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_INDEX_PATHS = 10_000
MAX_LOCK_BYTES = 4096
LOCK_STALE_SECONDS = 10 * 60
DEFAULT_TIME_BUDGET_SECONDS = 5.0
INDEX_COLUMNS = {"path", "title", "summary", "body", "project", "timestamp", "slug"}


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
    if _safe_kind(queue, state_root)[0] != "directory":
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
        try:
            _repair_runtime(state_path, repaired)
        except OSError as exc:
            repair_errors.setdefault("runtime", []).append(
                f"Runtime repair failed: {type(exc).__name__}"
            )
        try:
            if not _repair_leases(state_path, generated_at, repaired):
                repair_deferred.add("queue")
        except OSError as exc:
            repair_errors.setdefault("queue", []).append(
                f"Queue repair failed: {type(exc).__name__}"
            )
        index_before = _index_check(state_path, generated_at, deadline)
        if index_before["details"].get("repairable") and index_before["status"] != "ok":
            index_lock = state_path / "cache" / ".doctor-index.lock"
            lock_token = _acquire_lock(index_lock, state_path / "cache", generated_at)
            if lock_token is None:
                repair_deferred.add("index")
            else:
                try:
                    _rebuild_index(root_path, state_path)
                    index_after = _index_check(state_path, generated_at, deadline)
                    if index_after["status"] == "ok":
                        repaired.append({"action": "rebuild_index"})
                    else:
                        message = (
                            "Index repair failed: index was not created"
                            if index_after["details"].get("freshness") == "missing"
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
                    _release_lock(
                        index_lock, state_path / "cache", lock_token
                    )

    checks = [
        _environment_check(root_path, state_path),
        _runtime_check(state_path),
        _queue_check(state_path, generated_at, deadline),
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
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "overall_status": overall,
        "repaired": repaired,
        "checks": checks,
        "counts": counts,
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
