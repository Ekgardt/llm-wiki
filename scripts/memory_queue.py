"""Persistent queue for deferred memory-pipeline tasks.

When a memory script (compile_memory, flush_memory, etc.) needs an LLM
but no backend is currently available (no Codex CLI, no OpenCode server,
no Claude, no Ollama, no API key), the task is **enqueued** here instead
of being silently dropped.

The queue is drained by:
  - OpenCode plugin's session.created handler (uses OpenCode SDK)
  - Codex wrapper after memory capture (uses codex exec)
  - Claude Code SessionStart hook (uses claude-agent-sdk)
  - Manual `uv run python scripts/memory_queue.py drain`

Storage: `$LLM_WIKI_STATE_ROOT/run/queue/*.json`
Each file is one task, atomic via tmp+rename. Queue is crash-safe.

Task schema:
    {
      "id": "<YYYYMMDD-HHMMSS-8lowerhex>",
      "type": "compile" | "classify" | "lint_contradictions" | "query",
      "enqueued_at": "<iso8601>",
      "enqueue_sequence": 1,
      "attempts": 0,
      "last_attempt_at": null,
      "payload": { ... type-specific fields ... }
    }
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import stat
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

MAX_ATTEMPTS = 5
MAX_SDK_TOKENS = 100_000
RETRY_BACKOFF_SECONDS = 60
QUEUE_LOCK_TIMEOUT_SECONDS = 30.0
MAX_PROVENANCE_CHARS = 500
CURRENT_FLUSH_PROVENANCE_VERSION = 1
MAX_RECOVERED_PROJECT_ROOT_CHARS = MAX_PROVENANCE_CHARS
MAX_SDK_BRIDGE_STDIN_BYTES = 8 * 1024 * 1024
MAX_QUEUE_ENTRIES = 10_000
MAX_QUEUE_TASK_BYTES = 16 * 1024 * 1024
MAX_QUEUE_SEQUENCE_BYTES = 128
MAX_QUEUE_MIGRATION_BYTES = 4 * 1024 * 1024
MAX_QUEUE_JSON_DEPTH = 64
QUEUE_RETRY_SCHEMA_VERSION = 1
MAX_QUEUE_RETRY_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_QUEUE_RETRY_TASKS = 1_000
MAX_QUEUE_RETRY_BACKUP_BYTES = 512 * 1024 * 1024
QUEUE_RETRY_MANIFEST_NAME = "queue-retry-manifest.json"
QUEUE_RETRY_SEAL_NAME = "queue-retry-manifest.seal.json"
QUEUE_RETRY_BARRIER_NAME = "queue-retry-active.json"
SOURCE_PROVENANCE_FIELDS = (
    "session_id",
    "trigger",
    "project_slug",
    "project_root",
    "occurred_at",
)
PROVENANCE_FIELDS = (
    "event",
    *SOURCE_PROVENANCE_FIELDS,
    "enqueued_by",
)
_TASK_ID_PATTERN = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{8}")
_CAPTURE_ID_PATTERN = re.compile(r"[0-9a-f]{64}")

_QUEUE_THREAD_LOCK = threading.Lock()


class QueueIntegrityError(RuntimeError):
    """The queue cannot be inspected completely and must not be mutated."""


def _is_canonical_task_id(task_id: object) -> bool:
    return (
        isinstance(task_id, str)
        and _TASK_ID_PATTERN.fullmatch(task_id) is not None
    )


def _is_canonical_capture_id(capture_id: object) -> bool:
    return (
        isinstance(capture_id, str)
        and _CAPTURE_ID_PATTERN.fullmatch(capture_id) is not None
    )


def _validated_capture_id(payload: dict[str, Any]) -> str | None:
    if "capture_id" not in payload:
        return None
    capture_id = payload["capture_id"]
    if not _is_canonical_capture_id(capture_id):
        raise ValueError("capture_id must be canonical lowercase 64-hex")
    return capture_id


@dataclass(frozen=True)
class QueueInventory:
    entries: tuple[Path, ...]
    tasks: tuple[tuple[Path, dict[str, Any]], ...]


def _state_root() -> Path:
    env = os.environ.get("LLM_WIKI_STATE_ROOT")
    if env:
        if "\0" in env:
            raise ValueError("LLM_WIKI_STATE_ROOT contains NUL")
        return Path(env)
    try:
        from memory_state import STATE_ROOT

        return Path(STATE_ROOT)
    except Exception:  # noqa: BLE001
        vault = Path(
            os.environ.get("LLM_WIKI_ROOT", Path(__file__).resolve().parent.parent)
        )
        return vault.resolve()


def _queue_path() -> Path:
    return _state_root() / "run" / "queue"


def _queue_dir() -> Path:
    q = _queue_path()
    q.mkdir(parents=True, exist_ok=True)
    return q


def _daily_dir() -> Path:
    from memory_state import ROOT

    return ROOT / "knowledge" / "daily"


def _sequence_file(queue_dir: Path) -> Path:
    return queue_dir.parent / "queue-sequence"


def _migration_file(queue_dir: Path) -> Path:
    return queue_dir.parent / "queue-migration.json"


def _queue_retry_barrier_path() -> Path:
    return _queue_path().parent / QUEUE_RETRY_BARRIER_NAME


@contextmanager
def _queue_order_lock():
    """Serialize queue ordering changes across threads and processes."""
    from memory_state import advisory_file_lock

    queue_dir = _queue_dir()
    with _QUEUE_THREAD_LOCK:
        with advisory_file_lock(
            queue_dir.parent / "queue-order.lock",
            timeout=QUEUE_LOCK_TIMEOUT_SECONDS,
            description="memory queue ordering",
        ):
            yield queue_dir


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    from memory_state import atomic_write

    atomic_write(path, _queue_json_bytes(data).decode("utf-8"))


def _queue_json_bytes(data: object, *, canonical: bool = False) -> bytes:
    options = (
        {"sort_keys": True, "separators": (",", ":")}
        if canonical
        else {"indent": 2}
    )
    return (
        json.dumps(data, ensure_ascii=False, **options)
        + ("" if canonical else "\n")
    ).encode("utf-8", errors="strict")


def _sanitize_queue_payload(payload: dict[str, Any]) -> dict[str, Any]:
    from secret_redact import redact_secrets

    sanitized = dict(payload)
    for field in PROVENANCE_FIELDS:
        if field not in sanitized:
            continue
        redacted = redact_secrets(str(sanitized[field] or ""))
        sanitized[field] = " ".join(redacted.split())[:MAX_PROVENANCE_CHARS]
    return sanitized


def _integrity_error(message: str, path: Path | None = None) -> QueueIntegrityError:
    location = f": {path}" if path is not None else ""
    return QueueIntegrityError(f"queue integrity unavailable: {message}{location}")


def _is_reparse_point(metadata) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _read_bounded_regular_bytes(
    path: Path,
    *,
    max_bytes: int,
    label: str,
    allow_missing: bool = False,
) -> bytes | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise _integrity_error(f"{label} disappeared", path) from None
    except OSError as exc:
        raise _integrity_error(f"{label} metadata is unreadable", path) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(metadata)
    ):
        raise _integrity_error(f"{label} is not a regular file", path)
    if metadata.st_size > max_bytes:
        raise _integrity_error(
            f"{label} exceeds the {max_bytes} byte limit",
            path,
        )
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not os.path.samestat(metadata, opened)
                or not stat.S_ISREG(opened.st_mode)
                or _is_reparse_point(opened)
            ):
                raise _integrity_error(f"{label} changed before it could be read", path)
            raw = handle.read(max_bytes + 1)
    except QueueIntegrityError:
        raise
    except OSError as exc:
        raise _integrity_error(f"{label} is unreadable", path) from exc
    if len(raw) > max_bytes:
        raise _integrity_error(
            f"{label} exceeds the {max_bytes} byte limit",
            path,
        )
    return raw


def _reject_json_constant(value: str):
    raise ValueError(f"non-finite JSON number: {value}")


def _validate_json_graph(data: dict[str, Any], *, label: str, path: Path) -> None:
    pending: list[tuple[Any, int]] = [(data, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > MAX_QUEUE_JSON_DEPTH:
            raise _integrity_error(
                f"{label} exceeds the JSON depth limit of {MAX_QUEUE_JSON_DEPTH}",
                path,
            )
        if isinstance(value, str):
            if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
                raise _integrity_error(f"{label} contains a surrogate code point", path)
        elif isinstance(value, dict):
            pending.extend((key, depth + 1) for key in value)
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)


def _read_json_object(
    path: Path,
    *,
    max_bytes: int,
    label: str,
    allow_missing: bool = False,
) -> dict[str, Any] | None:
    raw = _read_bounded_regular_bytes(
        path,
        max_bytes=max_bytes,
        label=label,
        allow_missing=allow_missing,
    )
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8", errors="strict")
        data = json.loads(text, parse_constant=_reject_json_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise _integrity_error(f"{label} is not valid UTF-8 JSON", path) from exc
    if not isinstance(data, dict):
        raise _integrity_error(f"{label} must contain a top-level object", path)
    _validate_json_graph(data, label=label, path=path)
    return data


def _read_task_json(path: Path, *, allow_missing: bool = False) -> dict[str, Any] | None:
    task = _read_json_object(
        path,
        max_bytes=MAX_QUEUE_TASK_BYTES,
        label="queue task",
        allow_missing=allow_missing,
    )
    if task is None:
        return None
    invalid = not (
        isinstance(task.get("id"), str)
        and bool(task["id"])
        and isinstance(task.get("type"), str)
        and bool(task["type"])
        and isinstance(task.get("enqueued_at"), str)
        and type(task.get("attempts")) is int
        and task["attempts"] >= 0
        and (
            task.get("last_attempt_at") is None
            or isinstance(task.get("last_attempt_at"), str)
        )
        and isinstance(task.get("payload"), dict)
        and (
            "enqueue_sequence" not in task
            or (
                type(task["enqueue_sequence"]) is int
                and task["enqueue_sequence"] >= 1
            )
        )
        and (
            "_sdk_lease" not in task
            or isinstance(task.get("_sdk_lease"), dict)
        )
    )
    if not invalid:
        try:
            datetime.fromisoformat(task["enqueued_at"])
            if task.get("last_attempt_at") is not None:
                datetime.fromisoformat(task["last_attempt_at"])
        except ValueError:
            invalid = True
    if invalid:
        raise _integrity_error("queue task schema is invalid", path)
    if not _is_canonical_task_id(task["id"]):
        raise _integrity_error("queue task id is not canonical", path)
    if path.name != f"{task['id']}{path.suffix}":
        raise _integrity_error("queue task filename does not match its id", path)
    return task


def _inventory_queue_locked(queue_dir: Path) -> QueueInventory:
    entries: list[Path] = []
    tasks: list[tuple[Path, dict[str, Any]]] = []
    try:
        root_metadata = queue_dir.lstat()
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or stat.S_ISLNK(root_metadata.st_mode)
            or _is_reparse_point(root_metadata)
        ):
            raise _integrity_error(
                "queue directory is not a regular directory",
                queue_dir,
            )
        with os.scandir(queue_dir) as scanned:
            for count, entry in enumerate(scanned, start=1):
                if count > MAX_QUEUE_ENTRIES:
                    raise _integrity_error(
                        f"queue entry limit of {MAX_QUEUE_ENTRIES} was exceeded",
                        queue_dir,
                    )
                path = Path(entry.path)
                entries.append(path)
                suffix = path.suffix
                normalized_suffix = suffix.casefold()
                if (
                    normalized_suffix in {".json", ".processing"}
                    and suffix != normalized_suffix
                ):
                    raise _integrity_error(
                        "queue task suffix is not canonical lowercase",
                        path,
                    )
                if suffix not in {".json", ".processing"}:
                    continue
                task = _read_task_json(path)
                if task is None:  # pragma: no cover - required files cannot be absent
                    raise _integrity_error("queue task disappeared", path)
                tasks.append((path, task))
    except QueueIntegrityError:
        raise
    except OSError as exc:
        raise _integrity_error("queue directory cannot be inventoried", queue_dir) from exc
    return QueueInventory(tuple(entries), tuple(tasks))


def _read_sequence(queue_dir: Path) -> int:
    path = _sequence_file(queue_dir)
    try:
        raw = _read_bounded_regular_bytes(
            path,
            max_bytes=MAX_QUEUE_SEQUENCE_BYTES,
            label="queue sequence counter",
            allow_missing=True,
        )
        if raw is None:
            return 0
        value = int(raw.decode("utf-8", errors="strict").strip())
    except QueueIntegrityError:
        raise
    except (UnicodeError, ValueError) as exc:
        raise _integrity_error("queue sequence counter is invalid", path) from exc
    except FileNotFoundError:
        return 0
    if value < 0:
        raise _integrity_error("queue sequence counter is invalid", path)
    return value


def _write_sequence(queue_dir: Path, value: int) -> None:
    from memory_state import atomic_write

    atomic_write(_sequence_file(queue_dir), f"{value}\n")


def _read_pending_files(queue_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    inventory = _inventory_queue_locked(queue_dir)
    return [(path, task) for path, task in inventory.tasks if path.suffix == ".json"]


def _enqueue_evidence(path: Path, task: dict[str, Any]) -> tuple[float, int, str]:
    try:
        timestamp = datetime.fromisoformat(str(task["enqueued_at"])).timestamp()
    except (KeyError, TypeError, ValueError, OSError):
        timestamp = path.stat().st_mtime
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    return timestamp, mtime_ns, path.name


def _read_migration(queue_dir: Path) -> list[tuple[str, int]]:
    path = _migration_file(queue_dir)
    try:
        data = _read_json_object(
            path,
            max_bytes=MAX_QUEUE_MIGRATION_BYTES,
            label="queue migration journal",
            allow_missing=True,
        )
        if data is None:
            return []
        raw_assignments = data["assignments"]
        assignments = [
            (str(item["filename"]), int(item["sequence"]))
            for item in raw_assignments
        ]
    except QueueIntegrityError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise _integrity_error("queue migration journal is invalid", path) from exc
    if any(
        Path(filename).name != filename
        or not filename.endswith(".json")
        or not _is_canonical_task_id(filename.removesuffix(".json"))
        or sequence < 1
        for filename, sequence in assignments
    ):
        raise _integrity_error("queue migration journal is invalid", path)
    return assignments


def _apply_migration(
    queue_dir: Path, assignments: list[tuple[str, int]]
) -> list[tuple[Path, dict[str, Any]]]:
    for filename, sequence in assignments:
        path = queue_dir / filename
        task = _read_task_json(path, allow_missing=True)
        if task is None:
            continue
        if task.get("enqueue_sequence") != sequence:
            task["enqueue_sequence"] = sequence
            _atomic_write_json(path, task)

    counter = max(_read_sequence(queue_dir), *(sequence for _, sequence in assignments))
    _write_sequence(queue_dir, counter)
    migration_file = _migration_file(queue_dir)
    try:
        migration_file.unlink()
    except FileNotFoundError:
        pass
    else:
        from memory_state import _sync_parent_directory

        _sync_parent_directory(migration_file)
    return _read_pending_files(queue_dir)


def _migrate_legacy_tasks(
    queue_dir: Path, tasks: list[tuple[Path, dict[str, Any]]]
) -> list[tuple[Path, dict[str, Any]]]:
    """Assign deterministic sequences to queues created before sequencing."""
    assignments = _read_migration(queue_dir)
    if assignments:
        tasks = _apply_migration(queue_dir, assignments)

    counter = _read_sequence(queue_dir)
    if any("enqueue_sequence" not in task for _, task in tasks):
        tasks.sort(key=lambda item: _enqueue_evidence(*item))
        assignments = [
            (path.name, sequence)
            for sequence, (path, _) in enumerate(tasks, start=1)
        ]
        _atomic_write_json(
            _migration_file(queue_dir),
            {
                "version": 1,
                "assignments": [
                    {"filename": filename, "sequence": sequence}
                    for filename, sequence in assignments
                ],
            },
        )
        return _apply_migration(queue_dir, assignments)

    max_pending_sequence = max(
        (int(task["enqueue_sequence"]) for _, task in tasks), default=0
    )
    if max_pending_sequence > counter:
        _write_sequence(queue_dir, max_pending_sequence)
    return tasks


def _enqueue_locked(
    queue_dir: Path, task_type: str, payload: dict[str, Any]
) -> str:
    pending = _read_pending_files(queue_dir)
    _migrate_legacy_tasks(queue_dir, pending)
    sequence = _read_sequence(queue_dir) + 1
    _write_sequence(queue_dir, sequence)

    now = datetime.now()
    task_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    task = {
        "id": task_id,
        "type": task_type,
        "enqueued_at": now.isoformat(timespec="microseconds"),
        "enqueue_sequence": sequence,
        "attempts": 0,
        "last_attempt_at": None,
        "payload": payload,
    }
    _atomic_write_json(queue_dir / f"{task_id}.json", task)
    return task_id


def enqueue(task_type: str, payload: dict[str, Any]) -> str:
    """Add a task to the persistent queue. Returns the task id.

    Safe to call from any process — atomic file write via tmp+rename.
    """
    payload = _sanitize_queue_payload(payload)
    capture_id = _validated_capture_id(payload) if task_type == "flush" else None
    with _queue_order_lock() as queue_dir:
        if capture_id is not None:
            inventory = _inventory_queue_locked(queue_dir)
            pending = _migrate_legacy_tasks(
                queue_dir,
                [
                    (path, task)
                    for path, task in inventory.tasks
                    if path.suffix == ".json"
                ],
            )
            processing = [
                (path, task)
                for path, task in inventory.tasks
                if path.suffix == ".processing"
            ]
            matches = [
                (path, task)
                for path, task in (*pending, *processing)
                if task.get("type") == "flush"
                and isinstance(task.get("payload"), dict)
                and task["payload"].get("capture_id") == capture_id
            ]
            if matches:
                _path, task = min(
                    matches,
                    key=lambda item: (
                        int(item[1].get("enqueue_sequence", sys.maxsize)),
                        str(item[1].get("enqueued_at", "")),
                        item[0].name,
                    ),
                )
                return str(task["id"])
        return _enqueue_locked(queue_dir, task_type, payload)


def _has_pending_compile_work() -> bool:
    """Inspect atomically replaced state without taking the state lock.

    Some flush paths acquire the state lock before entering the queue, so queue
    settlement must not acquire that lock in the reverse order.
    """
    state_file = _queue_dir().parent / "state.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    if state.get("compile_index_pending"):
        return True

    daily_dir = _daily_dir()
    if not daily_dir.exists():
        return False
    from memory_state import trusted_compiled_daily_hashes

    compiled_hashes = trusted_compiled_daily_hashes(
        state,
        root=daily_dir.parent.parent,
    )
    return any(
        compiled_hashes.get(path.name)
        != hashlib.sha256(path.read_bytes()).hexdigest()
        for path in daily_dir.glob("*.md")
    )


def ensure_compile_task() -> dict[str, Any]:
    """Atomically reuse or create the sole durable compile control."""
    with _queue_order_lock() as queue_dir:
        inventory = _inventory_queue_locked(queue_dir)
        pending = [
            (path, task)
            for path, task in inventory.tasks
            if path.suffix == ".json"
        ]
        pending = _migrate_legacy_tasks(queue_dir, pending)
        controls = [
            (path, task)
            for path, task in (
                *pending,
                *(
                    (path, task)
                    for path, task in inventory.tasks
                    if path.suffix == ".processing"
                ),
            )
            if task.get("type") == "compile"
        ]
        if controls:
            path, task = min(
                controls,
                key=lambda item: (
                    int(item[1].get("enqueue_sequence", sys.maxsize)),
                    str(item[1].get("enqueued_at", "")),
                    item[0].name,
                ),
            )
            control = {
                "pending": True,
                "created": False,
                "task_id": task.get("id"),
            }
            if path.suffix == ".processing":
                control["state"] = "processing"
                return control
            if int(task.get("attempts", 0)) >= MAX_ATTEMPTS:
                control["state"] = "terminal"
                return control
            retry = _retry_schedule(task)
            if retry is not None:
                delay, eligible_at = retry
                control.update(
                    {
                        "state": "backoff",
                        "retry_delay_seconds": delay,
                        "eligible_at": eligible_at,
                    }
                )
                return control
            control["state"] = "pending_eligible"
            return control
        if not _has_pending_compile_work():
            return {
                "pending": False,
                "created": False,
                "task_id": None,
                "state": "not_needed",
            }
        task_id = _enqueue_locked(queue_dir, "compile", {"force": False})
        return {
            "pending": True,
            "created": True,
            "task_id": task_id,
            "state": "pending_eligible",
        }


def list_pending(max_age_days: int | None = None) -> list[dict[str, Any]]:
    """Return all pending tasks, oldest first.

    `max_age_days` filters out tasks older than N days (avoid infinite
    buildup of unservable tasks). None = no filter.
    """
    cutoff = None
    if max_age_days is not None:
        cutoff = datetime.now().timestamp() - (max_age_days * 86400)

    out: list[dict[str, Any]] = []
    with _queue_order_lock() as queue_dir:
        pending = _migrate_legacy_tasks(queue_dir, _read_pending_files(queue_dir))
        for path, task in pending:
            if cutoff is not None:
                try:
                    enq = datetime.fromisoformat(task["enqueued_at"]).timestamp()
                    if enq < cutoff:
                        continue
                except (KeyError, ValueError):
                    pass
            task["_path"] = str(path)
            out.append(task)
    out.sort(
        key=lambda task: (
            int(task["enqueue_sequence"]),
            str(task.get("enqueued_at", "")),
            task["_path"],
        )
    )
    return out


def mark_attempt(task_id: str, success: bool) -> None:
    """Update task's attempt counter (failure) or delete (success)."""
    if not _is_canonical_task_id(task_id):
        return
    with _queue_order_lock() as qdir:
        _inventory_queue_locked(qdir)
        path = qdir / f"{task_id}.json"
        task = _read_task_json(path, allow_missing=True)
        if task is None:
            return
        if success:
            try:
                path.unlink()
            except OSError:
                pass
            return
        task["attempts"] = int(task.get("attempts", 0)) + 1
        task["last_attempt_at"] = datetime.now().isoformat(timespec="seconds")
        _atomic_write_json(path, task)


def _recover_stale_leases_locked(qdir: Path, max_age_seconds: int) -> int:
    if not qdir.exists():
        return 0
    inventory = _inventory_queue_locked(qdir)
    cutoff = datetime.now().timestamp() - max_age_seconds
    recovered = 0
    entry_names = {path.name for path in inventory.entries}
    for marker in (
        path for path in inventory.entries if path.name.endswith(".acquiring")
    ):
        try:
            if marker.lstat().st_mtime < cutoff:
                marker.unlink()
                recovered += 1
        except OSError:
            pass
    for p, task in (
        item for item in inventory.tasks if item[0].suffix == ".processing"
    ):
        try:
            if p.with_suffix(".acquiring").name in entry_names:
                continue
            freshness = p.lstat().st_mtime
            lease = task.get("_sdk_lease", {})
            if not isinstance(lease, dict):
                raise _integrity_error("processing task lease is invalid", p)
            for field in ("leased_at", "renewed_at"):
                value = lease.get(field)
                if not value:
                    continue
                try:
                    stamp = datetime.fromisoformat(str(value))
                except (TypeError, ValueError) as exc:
                    raise _integrity_error(
                        f"processing task {field} is invalid",
                        p,
                    ) from exc
                if stamp.tzinfo is None:
                    stamp = stamp.astimezone()
                freshness = max(freshness, stamp.timestamp())
            if freshness < cutoff:
                target = p.with_suffix(".json")
                p.rename(target)
                recovered += 1
        except QueueIntegrityError:
            raise
        except OSError:
            pass
    return recovered


def recover_stale_leases(max_age_seconds: int = 600) -> int:
    """Recover stale queue leases while excluding claim and settlement."""
    with _queue_order_lock() as qdir:
        _queue_retry_require_no_barrier()
        return _recover_stale_leases_locked(qdir, max_age_seconds)


def _task_digest(task: dict[str, Any]) -> str:
    stable = {key: value for key, value in task.items() if key not in {"_path", "_sdk_lease"}}
    encoded = json.dumps(
        stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _retry_schedule(task: dict[str, Any]) -> tuple[int, str] | None:
    last = task.get("last_attempt_at")
    if not last:
        return None
    try:
        attempted_at = datetime.fromisoformat(last)
        eligible_at = attempted_at + timedelta(seconds=RETRY_BACKOFF_SECONDS)
        now = datetime.now(eligible_at.tzinfo) if eligible_at.tzinfo else datetime.now()
        remaining = (eligible_at - now).total_seconds()
        if remaining <= 0:
            return None
        delay = min(RETRY_BACKOFF_SECONDS, max(1, math.ceil(remaining)))
        return delay, eligible_at.isoformat(timespec="seconds")
    except (ValueError, TypeError):
        return None


def _retry_eligible(task: dict[str, Any]) -> bool:
    if int(task.get("attempts", 0)) >= MAX_ATTEMPTS:
        return False
    return _retry_schedule(task) is None


def _legacy_result_type(task_type: str, payload: dict[str, Any]) -> str | None:
    if task_type == "flush":
        return "flush"
    if task_type in {"query", "lint_contradictions"}:
        return "query"
    if task_type == "classify":
        if payload.get("event") or payload.get("enqueued_by") == "flush_memory":
            return "flush"
        return "query"
    return None


def _task_result_type(task: dict[str, Any]) -> str | None:
    payload = task.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    return _legacy_result_type(str(task.get("type") or ""), payload)


@dataclass(frozen=True)
class FlushProvenance:
    kind: str
    source_session_id: str | None = None
    project_slug: str = ""
    project_root: str = ""


def _provenance_string(
    payload: dict[str, Any],
    field: str,
    *,
    required: bool = False,
) -> str:
    if field not in payload:
        if required:
            raise ValueError(f"missing {field}")
        return ""
    value = payload[field]
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if value != value.strip() or len(value) > MAX_PROVENANCE_CHARS:
        raise ValueError(f"{field} is malformed")
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        raise ValueError(f"{field} is malformed")
    if required and not value:
        raise ValueError(f"missing {field}")
    return value


def _validate_occurrence(value: str) -> None:
    try:
        datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        )
    except ValueError as exc:
        raise ValueError("occurred_at is invalid") from exc


def _classify_flush_provenance(payload: dict[str, Any]) -> FlushProvenance:
    _validated_capture_id(payload)
    if "provenance_version" in payload:
        version = payload["provenance_version"]
        if type(version) is not int or version != CURRENT_FLUSH_PROVENANCE_VERSION:
            raise ValueError("unsupported provenance_version")
        values = {
            field: _provenance_string(payload, field, required=True)
            for field in PROVENANCE_FIELDS
        }
        if values["event"] not in {"session-end", "pre-compact"}:
            raise ValueError("event is invalid")
        if values["session_id"].casefold() == "unknown":
            raise ValueError("session_id is unavailable")
        if values["project_slug"].casefold() == "unknown":
            raise ValueError("project_slug is unavailable")
        if values["project_root"].casefold() == "unknown":
            raise ValueError("project_root is unavailable")
        if values["enqueued_by"] != "flush_memory":
            raise ValueError("enqueued_by is invalid")
        _validate_occurrence(values["occurred_at"])
        return FlushProvenance(
            "persisted",
            values["session_id"],
            values["project_slug"],
            values["project_root"],
        )

    if not any(field in payload for field in SOURCE_PROVENANCE_FIELDS):
        return FlushProvenance("legacy")

    values = {
        field: _provenance_string(payload, field)
        for field in PROVENANCE_FIELDS
        if field in payload
    }
    slug = values.get("project_slug", "")
    root = values.get("project_root", "")
    slug_missing = not slug or slug.casefold() == "unknown"
    root_missing = not root or root.casefold() == "unknown"
    if not slug_missing and not root_missing:
        return FlushProvenance(
            "persisted",
            values.get("session_id") or None,
            slug,
            root,
        )
    if slug_missing != root_missing:
        raise ValueError("project identity is partial")

    for field in ("event", "session_id", "trigger", "occurred_at", "enqueued_by"):
        values[field] = _provenance_string(payload, field, required=True)
    if values["event"] not in {"session-end", "pre-compact"}:
        raise ValueError("event is invalid")
    if values["session_id"].casefold() == "unknown":
        raise ValueError("session_id is unavailable")
    if values["enqueued_by"] != "flush_memory":
        raise ValueError("enqueued_by is invalid")
    _validate_occurrence(values["occurred_at"])
    return FlushProvenance("recovery", values["session_id"])


def _claim_next_task() -> tuple[dict[str, Any], Path] | None:
    """Atomically select and lease one eligible task under the order lock."""
    with _queue_order_lock() as queue_dir:
        _queue_retry_require_no_barrier()
        pending = _migrate_legacy_tasks(
            queue_dir, _read_pending_files(queue_dir)
        )
        pending.sort(
            key=lambda item: (
                item[1].get("type") == "compile",
                int(item[1]["enqueue_sequence"]),
                str(item[1].get("enqueued_at", "")),
                item[0].name,
            )
        )
        for path, _snapshot in pending:
            task = _read_task_json(path, allow_missing=True)
            if task is None:
                continue
            if not isinstance(task, dict) or not _retry_eligible(task):
                continue
            task_id = str(task.get("id") or "")
            if path.name != f"{task_id}.json":
                continue

            payload = task.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            result_type = _legacy_result_type(str(task.get("type") or ""), payload)
            marker = path.with_suffix(".acquiring")
            lease_path = path.with_suffix(".processing")
            if lease_path.exists():
                continue
            lease_id = uuid.uuid4().hex
            digest = _task_digest(task)
            try:
                fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except OSError:
                continue
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as claim:
                    json.dump(
                        {
                            "lease_id": lease_id,
                            "claimed_at": datetime.now().isoformat(),
                        },
                        claim,
                    )
                path.rename(lease_path)
                task["_sdk_lease"] = {
                    "id": lease_id,
                    "digest": digest,
                    "leased_at": datetime.now().isoformat(timespec="seconds"),
                    "result_type": result_type,
                }
                _atomic_write_json(lease_path, task)
            except OSError:
                if lease_path.exists() and not path.exists():
                    try:
                        lease_path.rename(path)
                    except OSError:
                        pass
                continue
            finally:
                try:
                    marker.unlink()
                except OSError:
                    pass
            return task, lease_path
    return None


def prepare_sdk_task() -> dict[str, Any]:
    """Lease and return one current or legacy queue task for SDK work."""
    recover_stale_leases()
    while True:
        claimed = _claim_next_task()
        if claimed is None:
            return {"pending": False}
        task, lease_path = claimed
        payload = task.get("payload")
        task_type = str(task.get("type", ""))
        payload = payload if isinstance(payload, dict) else {}
        lease = task["_sdk_lease"]
        result_type = lease.get("result_type")
        lease_id = str(lease["id"])
        digest = str(lease["digest"])
        if task_type == "compile":
            return {
                "pending": True,
                "kind": "compile",
                "task_id": task["id"],
                "lease_id": lease_id,
                "digest": digest,
                "type": task_type,
            }
        prompt = str(payload.get("prompt", "")).strip()
        if result_type is None or not prompt:
            try:
                reason = (
                    f"unsupported legacy task type: {task_type}"
                    if result_type is None
                    else f"legacy {task_type} task is missing prompt"
                )
                _release_sdk_failure(task, lease_path, reason, terminal=True)
            except OSError:
                pass
            continue
        max_tokens = payload.get("max_tokens", 4000)
        if (
            type(max_tokens) is not int
            or max_tokens < 1
            or max_tokens > MAX_SDK_TOKENS
        ):
            try:
                _release_sdk_failure(
                    task,
                    lease_path,
                    f"invalid max_tokens: expected integer from 1 to {MAX_SDK_TOKENS}",
                    terminal=True,
                )
            except OSError:
                pass
            continue
        provenance = None
        if result_type == "flush":
            try:
                provenance = _classify_flush_provenance(payload)
            except ValueError as exc:
                try:
                    _release_sdk_failure(
                        task,
                        lease_path,
                        f"invalid flush provenance: {exc}",
                    )
                except OSError:
                    pass
                continue

        prepared = {
            "pending": True,
            "kind": "sdk",
            "task_id": task["id"],
            "lease_id": lease_id,
            "digest": digest,
            "type": task_type,
            "result_type": result_type,
            "prompt": prompt,
            "system_prompt": payload.get("system_prompt", ""),
            "max_tokens": max_tokens,
        }
        if provenance is not None and provenance.kind == "recovery":
            prepared["recover_project_root"] = True
            prepared["source_session_id"] = provenance.source_session_id
        return prepared


def _lease_matches(task: dict[str, Any], lease_id: str, digest: str) -> bool:
    lease = task.get("_sdk_lease", {})
    return (
        lease.get("id") == lease_id
        and hmac.compare_digest(digest, str(lease.get("digest", "")))
        and hmac.compare_digest(digest, _task_digest(task))
    )


def renew_sdk_task(result: dict[str, Any]) -> tuple[bool, str]:
    """Renew a live SDK lease after validating its immutable identity."""
    if not isinstance(result, dict):
        return False, "invalid SDK lease renewal"
    task_id = str(result.get("task_id", ""))
    if not _is_canonical_task_id(task_id):
        return False, "stale or invalid SDK task id"
    lease_id = str(result.get("lease_id") or "")
    supplied_digest = str(result.get("digest", ""))
    with _queue_order_lock() as queue_dir:
        _inventory_queue_locked(queue_dir)
        lease_path = queue_dir / f"{task_id}.processing"
        task = _read_task_json(lease_path, allow_missing=True)
        if task is None:
            return False, "stale SDK task lease"
        if not _lease_matches(task, lease_id, supplied_digest):
            return False, "stale SDK task identity or digest"
        task["_sdk_lease"]["renewed_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        try:
            _atomic_write_json(lease_path, task)
        except OSError as exc:
            return False, f"failed to renew SDK task: {exc}"
    return True, "renewed"


def _release_sdk_failure_locked(
    task: dict[str, Any], lease_path: Path, error: str, *, terminal: bool = False
) -> None:
    pending_path = lease_path.with_suffix(".json")
    if pending_path.exists():
        raise FileExistsError(f"pending task already exists: {pending_path}")
    task.pop("_sdk_lease", None)
    task["attempts"] = (
        MAX_ATTEMPTS if terminal else int(task.get("attempts", 0)) + 1
    )
    task["last_attempt_at"] = datetime.now().isoformat(timespec="seconds")
    task["last_error"] = str(error or "SDK task failed")[:2000]
    if terminal:
        task["terminal_failure"] = True
    _atomic_write_json(lease_path, task)
    lease_path.rename(pending_path)


def _release_sdk_failure(
    task: dict[str, Any], lease_path: Path, error: str, *, terminal: bool = False
) -> None:
    lease = task.get("_sdk_lease", {})
    lease_id = str(lease.get("id") or "")
    digest = str(lease.get("digest") or "")
    with _queue_order_lock() as queue_dir:
        _inventory_queue_locked(queue_dir)
        current_path = queue_dir / lease_path.name
        current = _read_task_json(current_path, allow_missing=True)
        if current is None:
            raise OSError("stale SDK task lease")
        if not _lease_matches(current, lease_id, digest):
            raise OSError("stale SDK task identity or digest")
        _release_sdk_failure_locked(
            current, current_path, error, terminal=terminal
        )


def _defer_sdk_task_locked(task: dict[str, Any], lease_path: Path) -> None:
    pending_path = lease_path.with_suffix(".json")
    if pending_path.exists():
        raise FileExistsError(f"pending task already exists: {pending_path}")
    task.pop("_sdk_lease", None)
    _atomic_write_json(lease_path, task)
    lease_path.rename(pending_path)


def _settle_compile_success_locked(
    expected: dict[str, Any], lease_path: Path
) -> tuple[bool, str]:
    """Revalidate and settle a successful compile while the queue lock is held."""
    lease = expected.get("_sdk_lease", {})
    lease_id = str(lease.get("id") or "")
    digest = str(lease.get("digest") or "")
    current = _read_task_json(lease_path, allow_missing=True)
    if current is None:
        return False, "stale SDK task lease"
    if current.get("type") != "compile" or not _lease_matches(
        current, lease_id, digest
    ):
        return False, "stale SDK task identity or digest"

    from daily_log_append import _daily_lock

    # Daily writers release this lock before triggering queue work, so
    # queue -> daily is the only nested lock order.
    with _daily_lock(state_root=_state_root()):
        if _has_pending_compile_work():
            _defer_sdk_task_locked(current, lease_path)
            return True, "compile_pending"
        lease_path.unlink()
    return True, "acknowledged"


def _flush_task_marker(task_id: str) -> str:
    digest = hashlib.sha256(task_id.encode("utf-8", errors="replace")).hexdigest()
    return f"<!-- llm-wiki-queue-task: {digest} -->"


def _flush_capture_marker(capture_id: object) -> str:
    if not _is_canonical_capture_id(capture_id):
        raise ValueError("flush capture_id is not canonical")
    return f"<!-- llm-wiki-capture: {capture_id} -->"


def _declared_non_ok_flush_tier(response: str) -> str | None:
    stripped = str(response or "").strip()
    if not stripped:
        return None
    first_line = stripped.splitlines()[0].strip().upper().rstrip(".")
    while first_line.startswith("`") and first_line.endswith("`") and len(first_line) > 1:
        first_line = first_line[1:-1]
    return first_line if first_line in {"FLUSH_MAJOR", "FLUSH_MINOR"} else None


def _resolve_flush_identity(
    provenance: FlushProvenance,
    recovered_project_root: str | None,
) -> tuple[str, str]:
    from flush_memory import _resolve_project_identity

    if provenance.kind == "legacy":
        if recovered_project_root is not None:
            raise ValueError("recovered project root is not allowed")
        return "unknown", "unknown"
    if provenance.kind == "persisted":
        if recovered_project_root is not None:
            raise ValueError(
                "recovered project root cannot override persisted provenance"
            )
        identity = _resolve_project_identity(
            provenance.project_slug,
            provenance.project_root,
            env={},
        )
    else:
        if recovered_project_root is None:
            raise ValueError("recovered project root is required")
        identity = _resolve_project_identity(None, recovered_project_root, env={})
    if identity is None:
        raise ValueError("flush task project identity is unavailable")
    slug, root = identity
    canonical_root = "" if root is None else str(root)
    if (
        not canonical_root
        or canonical_root != canonical_root.strip()
        or len(canonical_root) > MAX_PROVENANCE_CHARS
        or any(
            ord(char) < 32 or 127 <= ord(char) <= 159
            for char in canonical_root
        )
    ):
        raise ValueError("flush task project identity is unavailable")
    return slug, canonical_root


def apply_classified_flush_response(
    task: dict[str, Any],
    response: str,
    *,
    recovered_project_root: str | None = None,
) -> Path | None:
    """Apply one deferred flush response idempotently from durable task metadata."""
    from daily_log_append import locked_append_once
    from flush_memory import (
        _classify_response,
        _is_valid_flush_ok_response,
        render_flush_block,
    )

    tier, body = _classify_response(response)
    if _declared_non_ok_flush_tier(response) and not body:
        raise ValueError("flush result has no distilled body")
    if tier == "ok":
        if not _is_valid_flush_ok_response(response):
            raise ValueError("flush result is not the exact FLUSH_OK token")
    elif not body:
        raise ValueError("flush result has no distilled body")

    task_id = task.get("id")
    if not _is_canonical_task_id(task_id):
        raise ValueError("flush task id is not canonical")
    payload = task.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    provenance = _classify_flush_provenance(payload)
    project_slug, project_root = _resolve_flush_identity(
        provenance,
        recovered_project_root,
    )
    if tier == "ok":
        return None
    capture_id = _validated_capture_id(payload)
    marker = (
        _flush_capture_marker(capture_id)
        if capture_id is not None
        else _flush_task_marker(task_id)
    )
    day, block = render_flush_block(
        tier,
        body,
        event=str(payload.get("event") or "session-end"),
        session_id=str(payload.get("session_id") or "unknown"),
        trigger=str(payload.get("trigger") or "unknown"),
        project_slug=project_slug,
        project_root=project_root,
        occurred_at=str(payload.get("occurred_at") or ""),
        deferred=True,
        idempotency_marker=marker,
    )
    daily_dir = _daily_dir()
    daily_path = daily_dir / f"{day}.md"
    return locked_append_once(daily_path, block, marker)


def _apply_sdk_response(
    task: dict[str, Any],
    response: str,
    *,
    recovered_project_root: str | None = None,
) -> None:
    payload = task.get("payload", {})
    task_type = task.get("type")
    result_type = _task_result_type(task)
    if task_type == "compile":
        if response != "COMPILE_COMPLETED":
            raise ValueError("compile control result is not validated completion")
        return
    if result_type == "query":
        results_dir = _queue_dir().parent / "queue-results"
        results_dir.mkdir(parents=True, exist_ok=True)
        out_path = payload.get("output_path")
        if out_path:
            target = Path(out_path).resolve()
            try:
                target.relative_to(results_dir.resolve())
            except ValueError as exc:
                raise ValueError("query output_path escapes queue-results") from exc
        else:
            target = results_dir / f"{task['id']}.txt"
        target.write_text(response, encoding="utf-8")
        return
    if result_type == "flush":
        if recovered_project_root is None:
            apply_classified_flush_response(task, response)
        else:
            apply_classified_flush_response(
                task,
                response,
                recovered_project_root=recovered_project_root,
            )
        return
    raise ValueError(f"unsupported SDK queue task type: {task.get('type')}")


def apply_sdk_result(result: dict[str, Any]) -> tuple[bool, str]:
    """Validate a leased SDK result, then acknowledge or durably fail it."""
    if not isinstance(result, dict):
        return False, "invalid SDK result"
    task_id = str(result.get("task_id", ""))
    if not _is_canonical_task_id(task_id):
        return False, "stale or invalid SDK task id"
    lease_id = str(result.get("lease_id") or "")
    supplied_digest = str(result.get("digest", ""))
    with _queue_order_lock() as queue_dir:
        _inventory_queue_locked(queue_dir)
        lease_path = queue_dir / f"{task_id}.processing"
        task = _read_task_json(lease_path, allow_missing=True)
        if task is None:
            return False, "stale SDK task lease"
        if not _lease_matches(task, lease_id, supplied_digest):
            return False, "stale SDK task identity or digest"

        schema_error = ""
        if "defer" in result and type(result["defer"]) is not bool:
            schema_error = "defer must be a bool"
        elif "success" in result and type(result["success"]) is not bool:
            schema_error = "success must be a bool"
        elif result.get("defer") is True and task.get("type") != "compile":
            schema_error = "only compile controls may be deferred"
        elif result.get("defer") is not True and "success" not in result:
            schema_error = "success must be a bool"
        elif result.get("success") is True and not isinstance(result.get("response"), str):
            schema_error = "response must be a string"

        recovered_root_present = "recovered_project_root" in result
        recovered_root: str | None = None
        result_type = _task_result_type(task)
        provenance = None
        if result_type == "flush":
            try:
                provenance = _classify_flush_provenance(task.get("payload", {}))
            except ValueError as exc:
                schema_error = f"invalid flush provenance: {exc}"

        if not schema_error and recovered_root_present:
            candidate = result["recovered_project_root"]
            if not isinstance(candidate, str):
                schema_error = "recovered_project_root must be a string"
            elif (
                not candidate
                or candidate != candidate.strip()
                or len(candidate) > MAX_RECOVERED_PROJECT_ROOT_CHARS
                or any(
                    ord(char) < 32 or 127 <= ord(char) <= 159
                    for char in candidate
                )
            ):
                schema_error = "recovered_project_root is malformed"
            else:
                recovered_root = candidate

        if (
            not schema_error
            and result.get("success") is not True
            and recovered_root_present
        ):
            schema_error = "failed results cannot include recovered_project_root"
        if not schema_error and result.get("success") is True:
            if provenance is not None and provenance.kind == "recovery":
                if recovered_root is None:
                    schema_error = "recovered_project_root is required"
            elif recovered_root_present:
                schema_error = "recovered_project_root is not allowed"
        if not schema_error and result_type != "flush" and recovered_root_present:
            schema_error = "recovered_project_root is not allowed"
        if schema_error:
            try:
                _release_sdk_failure_locked(
                    task, lease_path, f"invalid SDK result: {schema_error}"
                )
            except OSError as exc:
                return False, f"failed to persist SDK attempt: {exc}"
            return True, "failure recorded"

        if result.get("defer") is True:
            try:
                _defer_sdk_task_locked(task, lease_path)
            except OSError as exc:
                return False, f"failed to defer SDK task: {exc}"
            return True, "deferred"

        if not result.get("success"):
            try:
                _release_sdk_failure_locked(
                    task, lease_path, str(result.get("error", ""))
                )
            except OSError as exc:
                return False, f"failed to persist SDK attempt: {exc}"
            return True, "failure recorded"
        response = result["response"].strip()
        if not response:
            try:
                _release_sdk_failure_locked(task, lease_path, "empty SDK response")
            except OSError as exc:
                return False, f"failed to persist SDK attempt: {exc}"
            return True, "failure recorded"
        try:
            if recovered_root is None:
                _apply_sdk_response(task, response)
            else:
                _apply_sdk_response(
                    task,
                    response,
                    recovered_project_root=recovered_root,
                )
            if task.get("type") == "compile":
                settled, status = _settle_compile_success_locked(task, lease_path)
                if not settled:
                    return False, status
                return True, status
            else:
                lease_path.unlink()
        except Exception as exc:  # noqa: BLE001
            try:
                _release_sdk_failure_locked(
                    task,
                    lease_path,
                    f"apply failed: {type(exc).__name__}: {exc}",
                )
            except OSError as persist_exc:
                return False, f"failed to persist SDK apply error: {persist_exc}"
            return True, "failure recorded"
        return True, "acknowledged"


def _settle_manual_claim(
    task: dict[str, Any], lease_path: Path, *, success: bool, error: str = ""
) -> tuple[bool, str]:
    lease = task.get("_sdk_lease", {})
    lease_id = str(lease.get("id") or "")
    digest = str(lease.get("digest") or "")
    with _queue_order_lock() as queue_dir:
        _inventory_queue_locked(queue_dir)
        current_path = queue_dir / lease_path.name
        if success and task.get("type") == "compile":
            try:
                return _settle_compile_success_locked(task, current_path)
            except OSError:
                return False, "failed to settle manual compile task"
        current = _read_task_json(current_path, allow_missing=True)
        if current is None:
            return False, "stale manual task lease"
        if not _lease_matches(current, lease_id, digest):
            return False, "stale manual task identity or digest"
        try:
            if success:
                current_path.unlink()
            else:
                _release_sdk_failure_locked(current, current_path, error)
        except OSError:
            return False, "failed to settle manual task"
    return True, "acknowledged" if success else "failure recorded"


def drain_with(processor: Callable[[dict], bool], max_tasks: int = 10) -> dict[str, int]:
    """Drain the queue using a caller-provided processor.

    `processor(task)` must return True on success, False on failure. Successful
    noncompile tasks are deleted; compile controls are rechecked and may remain
    pending. Failures are re-queued with a bumped attempt count.

    Stops after `max_tasks` (default 10) to bound work per drain session.
    Returns counts: {"ok": N, "failed": M, "skipped": K, "pending": P}.

    Lease: each task file is renamed to ``.processing`` before the processor
    runs, so two concurrent drainers cannot pick up the same task. On
    success the ``.processing`` file is deleted or, for unfinished compile
    work, renamed back to ``.json``; on failure it is requeued and the attempt
    counter is bumped.
    """
    counts = {"ok": 0, "failed": 0, "skipped": 0, "pending": 0}
    recover_stale_leases()
    counts["skipped"] = sum(
        1 for task in list_pending() if not _retry_eligible(task)
    )
    claimed_count = 0
    while claimed_count < max(0, max_tasks):
        claimed = _claim_next_task()
        if claimed is None:
            break
        task, lease_path = claimed
        claimed_count += 1
        try:
            ok = bool(processor(task))
        except Exception as e:  # noqa: BLE001
            print(f"memory_queue: processor raised {type(e).__name__}: {e}", file=sys.stderr)
            ok = False
        if ok:
            settled, status = _settle_manual_claim(
                task, lease_path, success=True
            )
            if not settled:
                counts["failed"] += 1
            elif status == "compile_pending":
                counts["pending"] += 1
                break
            else:
                counts["ok"] += 1
        else:
            _settle_manual_claim(
                task,
                lease_path,
                success=False,
                error="manual queue processor failed",
            )
            counts["failed"] += 1
    return counts


def status() -> dict[str, Any]:
    """Snapshot of queue health — for the SessionStart metacognitive block."""
    with _queue_order_lock() as queue_dir:
        inventory = _inventory_queue_locked(queue_dir)
        pending_snapshot = [
            (path, task)
            for path, task in inventory.tasks
            if path.suffix == ".json"
        ]
        pending_pairs = _migrate_legacy_tasks(
            queue_dir, pending_snapshot
        )
        pending = [task for _, task in pending_pairs]
        in_flight = [
            task
            for path, task in inventory.tasks
            if path.suffix == ".processing"
        ]
    by_type: dict[str, int] = {}
    in_flight_by_type: dict[str, int] = {}
    failed_count = 0
    failed_ids: list[str] = []
    for t in pending:
        by_type[t.get("type", "unknown")] = by_type.get(t.get("type", "unknown"), 0) + 1
        if t.get("attempts", 0) >= MAX_ATTEMPTS:
            failed_count += 1
            task_id = str(t.get("id", ""))
            if _is_canonical_task_id(task_id):
                failed_ids.append(task_id)
    for task in in_flight:
        task_type = str(task.get("type", "unknown"))
        in_flight_by_type[task_type] = in_flight_by_type.get(task_type, 0) + 1
    return {
        "pending_total": len(pending),
        "by_type": by_type,
        "in_flight": len(in_flight),
        "in_flight_by_type": in_flight_by_type,
        "outstanding_total": len(pending) + len(in_flight),
        "permanently_failed": failed_count,
        "permanently_failed_ids": failed_ids,
        "queue_dir": str(_queue_dir()),
    }


def _queue_retry_digest(value: object) -> str:
    return hashlib.sha256(_queue_json_bytes(value, canonical=True)).hexdigest()


def _queue_retry_payload_digest(task: dict[str, Any]) -> str:
    return _queue_retry_digest(task["payload"])


def _queue_retry_manifest_digest(manifest: dict[str, Any]) -> str:
    return _queue_retry_digest(
        {key: value for key, value in manifest.items() if key != "approved"}
    )


def _queue_retry_write_private(path: Path, raw: bytes, *, bound=None) -> None:
    if bound is not None:
        bound.validate_path()
        if Path(os.path.abspath(path.parent)) != bound.path:
            raise QueueIntegrityError("queue retry artifact escaped its bound directory")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = (
        os.open(path.name, flags, 0o600, dir_fd=bound.descriptor)
        if bound is not None and bound.descriptor is not None
        else os.open(path, flags, 0o600)
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    metadata = (
        os.stat(path.name, dir_fd=bound.descriptor, follow_symlinks=False)
        if bound is not None and bound.descriptor is not None
        else path.lstat()
    )
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(metadata)
        or metadata.st_nlink != 1
    ):
        raise QueueIntegrityError(f"unsafe created queue retry artifact: {path}")
    if bound is not None:
        bound.validate_path()


def _queue_retry_read_private(path: Path, *, max_bytes: int, bound=None) -> bytes:
    if bound is not None:
        bound.validate_path()
        if Path(os.path.abspath(path.parent)) != bound.path:
            raise QueueIntegrityError("queue retry artifact escaped its bound directory")
    metadata = (
        os.stat(path.name, dir_fd=bound.descriptor, follow_symlinks=False)
        if bound is not None and bound.descriptor is not None
        else path.lstat()
    )
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(metadata)
        or metadata.st_nlink != 1
        or metadata.st_size > max_bytes
    ):
        raise QueueIntegrityError(f"unsafe queue retry artifact: {path}")
    descriptor = (
        os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=bound.descriptor,
        )
        if bound is not None and bound.descriptor is not None
        else os.open(
            path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    )
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not os.path.samestat(metadata, opened):
            raise QueueIntegrityError(f"queue retry artifact changed while opening: {path}")
        raw = handle.read(max_bytes + 1)
        finished = os.fstat(handle.fileno())
    current = (
        os.stat(path.name, dir_fd=bound.descriptor, follow_symlinks=False)
        if bound is not None and bound.descriptor is not None
        else path.lstat()
    )
    stable_fields = ("st_mode", "st_nlink", "st_size", "st_mtime_ns")
    if (
        len(raw) > max_bytes
        or not os.path.samestat(opened, finished)
        or not os.path.samestat(finished, current)
        or any(getattr(opened, field) != getattr(finished, field) for field in stable_fields)
        or any(getattr(finished, field) != getattr(current, field) for field in stable_fields)
    ):
        raise QueueIntegrityError(
            f"queue retry artifact changed or exceeded its bound: {path}"
        )
    if bound is not None:
        bound.validate_path()
    return raw


def _queue_retry_barrier(*, bound=None) -> dict[str, Any] | None:
    path = _queue_retry_barrier_path()
    try:
        raw = _queue_retry_read_private(path, max_bytes=4096, bound=bound)
    except FileNotFoundError:
        return None
    from memory_state import decode_json_object_strict

    barrier = decode_json_object_strict(raw, max_bytes=4096)
    if (
        set(barrier) != {"schema_version", "manifest", "manifest_sha256"}
        or barrier.get("schema_version") != QUEUE_RETRY_SCHEMA_VERSION
        or not isinstance(barrier.get("manifest"), str)
        or re.fullmatch(r"[0-9a-f]{64}", barrier.get("manifest_sha256", "")) is None
    ):
        raise QueueIntegrityError("queue retry transaction barrier is invalid")
    return barrier


def _queue_retry_require_no_barrier() -> None:
    if _queue_retry_barrier() is not None:
        raise QueueIntegrityError("queue retry transaction is incomplete")


def _queue_retry_publish_barrier(value: dict[str, Any]) -> None:
    from memory_state import (
        atomic_write,
        bind_atomic_writes_to_directory,
        require_absent_atomic_target,
    )

    path = _queue_retry_barrier_path()
    with bind_atomic_writes_to_directory(path.parent) as bound:
        with require_absent_atomic_target():
            atomic_write(path, _queue_json_bytes(value).decode("utf-8"))
        from memory_state import sync_parent_directory_strict

        sync_parent_directory_strict(path)
        if _queue_retry_barrier(bound=bound) != value:
            raise QueueIntegrityError("queue retry transaction barrier publication failed")


def _queue_retry_snapshot(path: Path) -> tuple[bytes, dict[str, Any], dict[str, int]]:
    metadata = path.lstat()
    if metadata.st_nlink != 1:
        raise _integrity_error("queue retry task has multiple hard links", path)
    raw = _read_bounded_regular_bytes(
        path,
        max_bytes=MAX_QUEUE_TASK_BYTES,
        label="queue retry task",
    )
    assert raw is not None
    from memory_state import decode_json_object_strict

    task = decode_json_object_strict(raw, max_bytes=MAX_QUEUE_TASK_BYTES)
    validated = _read_task_json(path)
    if validated != task:
        raise _integrity_error("queue retry task changed while validating", path)
    current = path.lstat()
    if (
        not os.path.samestat(metadata, current)
        or metadata.st_size != current.st_size
        or metadata.st_mtime_ns != current.st_mtime_ns
        or metadata.st_nlink != current.st_nlink
    ):
        raise _integrity_error("queue retry task identity changed", path)
    identity = {
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
        "mode": int(metadata.st_mode),
        "nlink": int(metadata.st_nlink),
    }
    return raw, task, identity


def _queue_retry_selected(
    queue_dir: Path,
    task_ids: list[str],
) -> list[tuple[Path, bytes, dict[str, Any], dict[str, int]]]:
    if (
        not task_ids
        or len(task_ids) > MAX_QUEUE_RETRY_TASKS
        or len(set(task_ids)) != len(task_ids)
    ):
        raise QueueIntegrityError("queue retry requires unique explicit task ids")
    if any(not _is_canonical_task_id(task_id) for task_id in task_ids):
        raise QueueIntegrityError("queue retry task id is not canonical")
    inventory = _inventory_queue_locked(queue_dir)
    selected = []
    for task_id in task_ids:
        matches = [
            (path, task)
            for path, task in inventory.tasks
            if task.get("id") == task_id
        ]
        if len(matches) != 1 or matches[0][0].suffix != ".json":
            raise QueueIntegrityError(
                f"queue retry task is missing, duplicated, or leased: {task_id}"
            )
        path, _task = matches[0]
        raw, task, identity = _queue_retry_snapshot(path)
        if task.get("attempts", 0) < MAX_ATTEMPTS:
            raise QueueIntegrityError(f"queue retry task is not exhausted: {task_id}")
        if type(task.get("enqueue_sequence")) is not int:
            raise QueueIntegrityError(
                f"queue retry task lacks a durable queue sequence: {task_id}"
            )
        selected.append((path, raw, task, identity))
    return selected


def retry_failed_audit(task_ids: list[str]) -> dict[str, Any]:
    try:
        _queue_retry_require_no_barrier()
        queue_dir = _queue_path()
        selected = _queue_retry_selected(queue_dir, task_ids)
        confirmed = _queue_retry_selected(queue_dir, task_ids)
        if [
            (path.name, raw, identity)
            for path, raw, _task, identity in selected
        ] != [
            (path.name, raw, identity)
            for path, raw, _task, identity in confirmed
        ]:
            raise QueueIntegrityError("queue retry audit snapshot drifted")
    except (OSError, ValueError, QueueIntegrityError) as exc:
        return {
            "schema_version": QUEUE_RETRY_SCHEMA_VERSION,
            "status": "ineligible",
            "eligible": False,
            "diagnostic": str(exc)[:500],
        }
    return {
        "schema_version": QUEUE_RETRY_SCHEMA_VERSION,
        "status": "eligible",
        "eligible": True,
        "task_ids": [task[2]["id"] for task in selected],
    }


def retry_failed_backup_only(task_ids: list[str]) -> dict[str, Any]:
    with _queue_order_lock() as queue_dir:
        _queue_retry_require_no_barrier()
        selected = _queue_retry_selected(queue_dir, task_ids)
        created_at = datetime.now(timezone.utc)
        entries = []
        artifacts = []
        aggregate_bytes = 0
        for path, before_raw, before_task, identity in selected:
            after_task = json.loads(json.dumps(before_task))
            after_task["attempts"] = 0
            after_task["last_attempt_at"] = None
            after_raw = _queue_json_bytes(after_task)
            if len(after_raw) > MAX_QUEUE_TASK_BYTES:
                raise QueueIntegrityError(
                    f"queue retry postimage exceeds its bound: {before_task['id']}"
                )
            before_name = f"queue-retry-tasks/{before_task['id']}.before.json"
            after_name = f"queue-retry-tasks/{before_task['id']}.after.json"
            aggregate_bytes += len(before_raw) + len(after_raw)
            if aggregate_bytes > MAX_QUEUE_RETRY_BACKUP_BYTES:
                raise QueueIntegrityError("queue retry backup exceeds its aggregate bound")
            artifacts.extend(((before_name, before_raw), (after_name, after_raw)))
            entries.append(
                {
                    "task_id": before_task["id"],
                    "task_type": before_task["type"],
                    "filename": path.name,
                    "enqueue_sequence": before_task["enqueue_sequence"],
                    "attempts": before_task["attempts"],
                    "last_error": before_task.get("last_error"),
                    "payload_identity_sha256": _queue_retry_payload_digest(before_task),
                    "identity": identity,
                    "before_artifact": before_name,
                    "before_sha256": hashlib.sha256(before_raw).hexdigest(),
                    "before_size": len(before_raw),
                    "before_digest": _task_digest(before_task),
                    "after_artifact": after_name,
                    "after_sha256": hashlib.sha256(after_raw).hexdigest(),
                    "after_size": len(after_raw),
                    "after_digest": _task_digest(after_task),
                }
            )
        manifest = {
            "schema_version": QUEUE_RETRY_SCHEMA_VERSION,
            "kind": "queue_retry_failed",
            "approved": False,
            "created_at": created_at.isoformat(),
            "state_root": str(_state_root().resolve()),
            "tasks": entries,
        }
        manifest_raw = _queue_json_bytes(manifest)
        if len(manifest_raw) > MAX_QUEUE_RETRY_MANIFEST_BYTES:
            raise QueueIntegrityError("queue retry manifest exceeds its bound")

        run_dir = queue_dir.parent
        for directory in (_state_root(), run_dir):
            metadata = directory.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or _is_reparse_point(metadata)
            ):
                raise QueueIntegrityError("queue retry runtime directory is unsafe")
        backups = run_dir / "backups"
        backups.mkdir(mode=0o700, exist_ok=True)
        backup_metadata = backups.lstat()
        if (
            not stat.S_ISDIR(backup_metadata.st_mode)
            or stat.S_ISLNK(backup_metadata.st_mode)
            or _is_reparse_point(backup_metadata)
        ):
            raise QueueIntegrityError("queue retry backup directory is unsafe")
        transaction = backups / (
            created_at.strftime("%Y%m%dT%H%M%S.%fZ") + f"-{uuid.uuid4().hex[:8]}"
        )
        from memory_state import (
            bind_atomic_writes_to_directory,
            sync_parent_directory_strict,
        )

        with bind_atomic_writes_to_directory(backups):
            transaction.mkdir(mode=0o700)
            sync_parent_directory_strict(transaction)
            with bind_atomic_writes_to_directory(transaction) as transaction_bound:
                task_artifacts = transaction / "queue-retry-tasks"
                task_artifacts.mkdir(mode=0o700)
                sync_parent_directory_strict(task_artifacts)
                with bind_atomic_writes_to_directory(task_artifacts) as artifact_bound:
                    for relative_name, raw in artifacts:
                        _queue_retry_write_private(
                            transaction / relative_name,
                            raw,
                            bound=artifact_bound,
                        )
                    sync_parent_directory_strict(task_artifacts / "artifact")
                manifest_path = transaction / QUEUE_RETRY_MANIFEST_NAME
                _queue_retry_write_private(
                    manifest_path,
                    manifest_raw,
                    bound=transaction_bound,
                )
                _queue_retry_write_private(
                    transaction / QUEUE_RETRY_SEAL_NAME,
                    _queue_json_bytes(
                        {"sha256": _queue_retry_manifest_digest(manifest)}
                    ),
                    bound=transaction_bound,
                )
                sync_parent_directory_strict(transaction / "artifact")
        return {
            "schema_version": QUEUE_RETRY_SCHEMA_VERSION,
            "status": "prepared",
            "manifest": str(manifest_path),
        }


def _queue_retry_manifest_path(path: Path) -> Path:
    candidate = path.absolute()
    backups = (_queue_path().parent / "backups").absolute()
    if (
        candidate.name != QUEUE_RETRY_MANIFEST_NAME
        or candidate.parent.parent != backups
        or re.fullmatch(r"[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-[0-9a-f]{8}", candidate.parent.name)
        is None
    ):
        raise QueueIntegrityError("queue retry manifest is outside its backup transaction")
    for directory in (backups, candidate.parent):
        metadata = directory.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or _is_reparse_point(metadata)
        ):
            raise QueueIntegrityError("queue retry manifest directory is unsafe")
    return candidate


def _load_queue_retry_manifest(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidate = _queue_retry_manifest_path(path)
    from memory_state import bind_atomic_writes_to_directory

    with ExitStack() as stack:
        transaction_bound = stack.enter_context(
            bind_atomic_writes_to_directory(candidate.parent)
        )
        artifact_bound = stack.enter_context(
            bind_atomic_writes_to_directory(candidate.parent / "queue-retry-tasks")
        )
        return _load_queue_retry_manifest_files(
            candidate,
            transaction_bound,
            artifact_bound,
        )


def _load_queue_retry_manifest_files(
    candidate: Path,
    transaction_bound,
    artifact_bound,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from memory_state import decode_json_object_strict

    manifest = decode_json_object_strict(
        _queue_retry_read_private(
            candidate,
            max_bytes=MAX_QUEUE_RETRY_MANIFEST_BYTES,
            bound=transaction_bound,
        ),
        max_bytes=MAX_QUEUE_RETRY_MANIFEST_BYTES,
    )
    if set(manifest) != {
        "schema_version",
        "kind",
        "approved",
        "created_at",
        "state_root",
        "tasks",
    } or (
        manifest.get("schema_version") != QUEUE_RETRY_SCHEMA_VERSION
        or manifest.get("kind") != "queue_retry_failed"
        or type(manifest.get("approved")) is not bool
        or manifest.get("state_root") != str(_state_root().resolve())
        or not isinstance(manifest.get("tasks"), list)
        or not manifest["tasks"]
        or len(manifest["tasks"]) > MAX_QUEUE_RETRY_TASKS
    ):
        raise QueueIntegrityError("queue retry manifest schema is invalid")
    try:
        datetime.fromisoformat(manifest["created_at"])
    except (TypeError, ValueError) as exc:
        raise QueueIntegrityError("queue retry manifest timestamp is invalid") from exc
    artifact_directory = candidate.parent / "queue-retry-tasks"
    artifact_metadata = artifact_directory.lstat()
    if (
        not stat.S_ISDIR(artifact_metadata.st_mode)
        or stat.S_ISLNK(artifact_metadata.st_mode)
        or _is_reparse_point(artifact_metadata)
    ):
        raise QueueIntegrityError("queue retry artifact directory is unsafe")
    seal = decode_json_object_strict(
        _queue_retry_read_private(
            candidate.parent / QUEUE_RETRY_SEAL_NAME,
            max_bytes=1024,
            bound=transaction_bound,
        ),
        max_bytes=1024,
    )
    if set(seal) != {"sha256"} or not hmac.compare_digest(
        str(seal.get("sha256") or ""),
        _queue_retry_manifest_digest(manifest),
    ):
        raise QueueIntegrityError("queue retry manifest seal is invalid")
    expected_entry_keys = {
        "task_id",
        "task_type",
        "filename",
        "enqueue_sequence",
        "attempts",
        "last_error",
        "payload_identity_sha256",
        "identity",
        "before_artifact",
        "before_sha256",
        "before_size",
        "before_digest",
        "after_artifact",
        "after_sha256",
        "after_size",
        "after_digest",
    }
    seen: set[str] = set()
    loaded = []
    aggregate_bytes = 0
    for entry in manifest["tasks"]:
        if not isinstance(entry, dict) or set(entry) != expected_entry_keys:
            raise QueueIntegrityError("queue retry manifest task entry is invalid")
        task_id = entry.get("task_id")
        identity = entry.get("identity")
        if (
            not _is_canonical_task_id(task_id)
            or task_id in seen
            or not isinstance(entry.get("task_type"), str)
            or not entry["task_type"]
            or type(entry.get("enqueue_sequence")) is not int
            or entry["enqueue_sequence"] < 1
            or type(entry.get("attempts")) is not int
            or entry["attempts"] < MAX_ATTEMPTS
            or (
                entry.get("last_error") is not None
                and not isinstance(entry.get("last_error"), str)
            )
            or re.fullmatch(r"[0-9a-f]{64}", entry.get("payload_identity_sha256", ""))
            is None
            or not isinstance(identity, dict)
            or set(identity) != {"device", "inode", "mode", "nlink"}
            or any(type(value) is not int for value in identity.values())
            or identity["nlink"] != 1
        ):
            raise QueueIntegrityError("queue retry manifest task identity is invalid")
        seen.add(task_id)
        expected_before = f"queue-retry-tasks/{task_id}.before.json"
        expected_after = f"queue-retry-tasks/{task_id}.after.json"
        if (
            entry.get("filename") != f"{task_id}.json"
            or entry.get("before_artifact") != expected_before
            or entry.get("after_artifact") != expected_after
            or type(entry.get("before_size")) is not int
            or not 0 <= entry["before_size"] <= MAX_QUEUE_TASK_BYTES
            or type(entry.get("after_size")) is not int
            or not 0 <= entry["after_size"] <= MAX_QUEUE_TASK_BYTES
            or any(
                re.fullmatch(r"[0-9a-f]{64}", entry.get(field, "")) is None
                for field in (
                    "before_sha256",
                    "before_digest",
                    "after_sha256",
                    "after_digest",
                )
            )
        ):
            raise QueueIntegrityError("queue retry artifact path is invalid")
        before_raw = _queue_retry_read_private(
            candidate.parent / expected_before,
            max_bytes=MAX_QUEUE_TASK_BYTES,
            bound=artifact_bound,
        )
        after_raw = _queue_retry_read_private(
            candidate.parent / expected_after,
            max_bytes=MAX_QUEUE_TASK_BYTES,
            bound=artifact_bound,
        )
        aggregate_bytes += len(before_raw) + len(after_raw)
        if aggregate_bytes > MAX_QUEUE_RETRY_BACKUP_BYTES:
            raise QueueIntegrityError("queue retry backup exceeds its aggregate bound")
        before_task = decode_json_object_strict(before_raw, max_bytes=MAX_QUEUE_TASK_BYTES)
        after_task = decode_json_object_strict(after_raw, max_bytes=MAX_QUEUE_TASK_BYTES)
        if (
            len(before_raw) != entry.get("before_size")
            or len(after_raw) != entry.get("after_size")
            or not hmac.compare_digest(hashlib.sha256(before_raw).hexdigest(), entry["before_sha256"])
            or not hmac.compare_digest(hashlib.sha256(after_raw).hexdigest(), entry["after_sha256"])
            or _task_digest(before_task) != entry.get("before_digest")
            or _task_digest(after_task) != entry.get("after_digest")
            or before_task.get("id") != task_id
            or before_task.get("type") != entry["task_type"]
            or before_task.get("enqueue_sequence") != entry["enqueue_sequence"]
            or before_task.get("attempts") != entry["attempts"]
            or before_task.get("last_error") != entry["last_error"]
            or _queue_retry_payload_digest(before_task)
            != entry["payload_identity_sha256"]
        ):
            raise QueueIntegrityError("queue retry artifact digest is invalid")
        expected_after_task = json.loads(json.dumps(before_task))
        expected_after_task["attempts"] = 0
        expected_after_task["last_attempt_at"] = None
        if after_task != expected_after_task or before_task.get("attempts", 0) < MAX_ATTEMPTS:
            raise QueueIntegrityError("queue retry postimage is invalid")
        loaded.append(
            {
                "entry": entry,
                "before_raw": before_raw,
                "before_task": before_task,
                "after_raw": after_raw,
                "after_task": after_task,
            }
        )
    return manifest, loaded


def retry_failed_apply(manifest_path: Path) -> dict[str, Any]:
    manifest, loaded = _load_queue_retry_manifest(manifest_path)
    if manifest["approved"] is not True:
        raise QueueIntegrityError("queue retry manifest is not approved")
    from memory_state import bind_atomic_writes_to_directory

    with _queue_order_lock() as queue_dir:
        with ExitStack() as stack:
            run_bound = stack.enter_context(
                bind_atomic_writes_to_directory(queue_dir.parent)
            )
            stack.enter_context(bind_atomic_writes_to_directory(queue_dir))
            return _retry_failed_apply_locked(
                manifest_path,
                manifest,
                loaded,
                queue_dir,
                run_bound,
            )


def _retry_failed_apply_locked(
    manifest_path: Path,
    manifest: dict[str, Any],
    loaded: list[dict[str, Any]],
    queue_dir: Path,
    run_bound,
) -> dict[str, Any]:
    manifest_identity = {
        "schema_version": QUEUE_RETRY_SCHEMA_VERSION,
        "manifest": str(_queue_retry_manifest_path(manifest_path)),
        "manifest_sha256": _queue_retry_manifest_digest(manifest),
    }
    active_barrier = _queue_retry_barrier(bound=run_bound)
    if active_barrier is not None and active_barrier != manifest_identity:
        raise QueueIntegrityError("another queue retry transaction is incomplete")
    if active_barrier is not None:
        from memory_state import sync_file_strict, sync_parent_directory_strict

        barrier_path = _queue_retry_barrier_path()
        sync_file_strict(barrier_path)
        sync_parent_directory_strict(barrier_path)
        if _queue_retry_barrier(bound=run_bound) != manifest_identity:
            raise QueueIntegrityError("queue retry transaction barrier drifted")
    inventory = _inventory_queue_locked(queue_dir)
    states = []
    for item in loaded:
        entry = item["entry"]
        matches = [
            path
            for path, task in inventory.tasks
            if task.get("id") == entry["task_id"]
        ]
        if len(matches) != 1 or matches[0].suffix != ".json":
            raise QueueIntegrityError(
                f"queue retry task drifted or became leased: {entry['task_id']}"
            )
        path = matches[0]
        current_raw, _current_task, identity = _queue_retry_snapshot(path)
        if hmac.compare_digest(current_raw, item["after_raw"]):
            states.append((path, item, "after"))
            continue
        if not hmac.compare_digest(current_raw, item["before_raw"]):
            raise QueueIntegrityError(f"queue retry task drift: {entry['task_id']}")
        if identity != entry["identity"]:
            raise QueueIntegrityError(f"queue retry task identity drift: {entry['task_id']}")
        states.append((path, item, "before"))
    has_preimages = any(
        current_state == "before" for _path, _item, current_state in states
    )
    if has_preimages and active_barrier is None:
        _queue_retry_publish_barrier(manifest_identity)
        active_barrier = manifest_identity
    changed = active_barrier is not None
    for path, item, current_state in states:
        if current_state == "before":
            _atomic_write_json(path, item["after_task"])
            changed = True
        from memory_state import sync_file_strict, sync_parent_directory_strict

        sync_file_strict(path)
        sync_parent_directory_strict(path)
        published = _read_bounded_regular_bytes(
            path,
            max_bytes=MAX_QUEUE_TASK_BYTES,
            label="queue retry published task",
        )
        if published is None or not hmac.compare_digest(published, item["after_raw"]):
            raise QueueIntegrityError("queue retry task postimage verification failed")
    if active_barrier is not None:
        if _queue_retry_barrier(bound=run_bound) != manifest_identity:
            raise QueueIntegrityError("queue retry transaction barrier drifted")
        barrier_path = _queue_retry_barrier_path()
        if run_bound.descriptor is not None:
            os.unlink(barrier_path.name, dir_fd=run_bound.descriptor)
        else:
            barrier_path.unlink()
        run_bound.validate_path()
        from memory_state import sync_parent_directory_strict

        sync_parent_directory_strict(barrier_path)
    return {"status": "applied" if changed else "already_applied"}


def retry_failed_verify(manifest_path: Path) -> dict[str, Any]:
    manifest, loaded = _load_queue_retry_manifest(manifest_path)
    if manifest["approved"] is not True:
        raise QueueIntegrityError("queue retry manifest is not approved")
    from memory_state import bind_atomic_writes_to_directory

    with _queue_order_lock() as queue_dir:
        with ExitStack() as stack:
            stack.enter_context(bind_atomic_writes_to_directory(queue_dir.parent))
            stack.enter_context(bind_atomic_writes_to_directory(queue_dir))
            _queue_retry_require_no_barrier()
            inventory = _inventory_queue_locked(queue_dir)
            for item in loaded:
                task_id = item["entry"]["task_id"]
                matches = [
                    path
                    for path, task in inventory.tasks
                    if task.get("id") == task_id
                ]
                if not matches:
                    raise QueueIntegrityError(
                        f"queue retry verification found missing task: {task_id}"
                    )
                if len(matches) != 1 or matches[0].suffix != ".json":
                    raise QueueIntegrityError(
                        f"queue retry verification found an active lease: {task_id}"
                    )
                raw, _task, _identity = _queue_retry_snapshot(matches[0])
                if not hmac.compare_digest(raw, item["after_raw"]):
                    raise QueueIntegrityError(
                        f"queue retry verification drift: {task_id}"
                    )
    return {"status": "verified"}


# ---------------------------------------------------------------------------
# CLI for manual drain / inspection
# ---------------------------------------------------------------------------


def _cli() -> int:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument(
        "command",
        nargs="?",
        choices=["list", "status", "drain", "clear-failed", "retry-failed"],
    )
    bridge = p.add_mutually_exclusive_group()
    bridge.add_argument("--prepare-sdk-task", action="store_true")
    bridge.add_argument("--apply-sdk-result", action="store_true")
    bridge.add_argument("--renew-sdk-task", action="store_true")
    bridge.add_argument("--ensure-compile-task", action="store_true")
    p.add_argument(
        "--phase",
        choices=["audit", "backup-only", "apply", "verify"],
    )
    p.add_argument("--task-id", action="append", default=[])
    p.add_argument("--manifest")
    args = p.parse_args()

    if args.command == "retry-failed":
        phase = args.phase
        invalid = (
            phase is None
            or (phase in {"audit", "backup-only"} and (not args.task_id or args.manifest))
            or (phase in {"apply", "verify"} and (args.task_id or not args.manifest))
        )
        if invalid:
            print("memory_queue: invalid retry-failed arguments", file=sys.stderr)
            return 2
        try:
            if phase == "audit":
                result = retry_failed_audit(args.task_id)
            elif phase == "backup-only":
                result = retry_failed_backup_only(args.task_id)
            elif phase == "apply":
                result = retry_failed_apply(Path(args.manifest))
            else:
                result = retry_failed_verify(Path(args.manifest))
        except (OSError, ValueError, QueueIntegrityError) as exc:
            print(f"memory_queue: retry-failed drift: {exc}", file=sys.stderr)
            return 3
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("status") != "ineligible" else 3

    if args.phase is not None or args.task_id or args.manifest is not None:
        print("memory_queue: retry options require retry-failed", file=sys.stderr)
        return 2

    if args.prepare_sdk_task:
        print(json.dumps(prepare_sdk_task(), ensure_ascii=False))
        return 0

    if args.apply_sdk_result:
        from memory_state import read_json_object_bounded

        result = read_json_object_bounded(
            sys.stdin,
            max_bytes=MAX_SDK_BRIDGE_STDIN_BYTES,
        )
        if result is None:
            print("memory_queue: invalid SDK result", file=sys.stderr)
            return 2
        ok, message = apply_sdk_result(result)
        if not ok:
            print(f"memory_queue: {message}", file=sys.stderr)
            return 2
        print(json.dumps({"ok": True, "status": message}))
        return 0

    if args.renew_sdk_task:
        from memory_state import read_json_object_bounded

        result = read_json_object_bounded(
            sys.stdin,
            max_bytes=MAX_SDK_BRIDGE_STDIN_BYTES,
        )
        if result is None:
            print("memory_queue: invalid SDK lease renewal", file=sys.stderr)
            return 2
        ok, message = renew_sdk_task(result)
        if not ok:
            print(f"memory_queue: {message}", file=sys.stderr)
            return 2
        print(json.dumps({"ok": True, "status": message}))
        return 0

    if args.ensure_compile_task:
        print(json.dumps(ensure_compile_task(), ensure_ascii=False))
        return 0

    if args.command is None:
        p.error("a command or SDK bridge option is required")

    if args.command == "list":
        for t in list_pending():
            print(
                f"  {t['id']}  type={t['type']}  attempts={t.get('attempts', 0)}  "
                f"enqueued={t['enqueued_at']}"
            )
        return 0

    if args.command == "status":
        s = status()
        print(json.dumps(s, indent=2, ensure_ascii=False))
        return 0

    if args.command == "drain":
        # Manual drain uses llm_client (auto-detect backend).
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            from llm_client import call_llm
        except ImportError:
            print("llm_client not available", file=sys.stderr)
            return 2

        def processor(task: dict) -> bool:
            task_type = task.get("type")
            payload = task.get("payload", {})
            if task_type == "query":
                # Free-form LLM call. Prefer writing to output_path when set;
                # otherwise store under run/queue-results/<id>.txt so llm_client
                # enqueues (no output_path) are still drainable.
                prompt = payload.get("prompt", "")
                sys_prompt = payload.get("system_prompt", "")
                out_path = payload.get("output_path")
                max_tokens = int(payload.get("max_tokens") or 4000)
                if not prompt:
                    return False
                result = call_llm(prompt, sys_prompt, max_tokens=max_tokens)
                if not result:
                    return False
                results_dir = _queue_dir().parent / "queue-results"
                results_dir.mkdir(parents=True, exist_ok=True)
                if not out_path:
                    out_path = str(results_dir / f"{task.get('id', 'query')}.txt")
                else:
                    out_resolved = Path(out_path).resolve()
                    try:
                        out_resolved.relative_to(results_dir.resolve())
                    except ValueError:
                        print(
                            f"queue: output_path {out_path} escapes results dir, skipping",
                            file=sys.stderr,
                        )
                        return False
                try:
                    Path(out_path).write_text(result, encoding="utf-8")
                    return True
                except OSError:
                    return False
            if _legacy_result_type(str(task_type or ""), payload) == "flush":
                # Deferred flush from flush_memory: call the LLM, classify
                # the response, and apply the result to the daily log so
                # the session content is not silently lost.
                prompt = payload.get("prompt", "")
                sys_prompt = payload.get("system_prompt", "")
                max_tokens = int(payload.get("max_tokens") or 1500)
                if not prompt:
                    return False
                result = call_llm(prompt, sys_prompt, max_tokens=max_tokens)
                if not result:
                    return False
                try:
                    apply_classified_flush_response(task, result)
                    return True
                except Exception as e:  # noqa: BLE001
                    print(f"queue: flush apply failed: {type(e).__name__}: {e}", file=sys.stderr)
                    return False
            if task_type == "compile":
                try:
                    command = [
                        sys.executable,
                        str(Path(__file__).resolve().parent / "compile_memory.py"),
                        "--trigger",
                        "manual",
                    ]
                    completed = subprocess.run(
                        command,
                        cwd=Path(__file__).resolve().parent.parent,
                        check=False,
                    )
                    return completed.returncode == 0
                except Exception as exc:  # noqa: BLE001
                    print(f"  compile drain failed: {exc}", file=sys.stderr)
                    return False
            # Other task types (classify) have richer Python-side logic.
            print(
                f"  skipping {task['id']}: type={task_type} not supported in manual drain",
                file=sys.stderr,
            )
            return False

        counts = drain_with(processor, max_tasks=20)
        print(f"drain complete: {counts}")
        return 0

    if args.command == "clear-failed":
        cleared = 0
        with _queue_order_lock() as queue_dir:
            _queue_retry_require_no_barrier()
            inventory = _inventory_queue_locked(queue_dir)
            for path, task in inventory.tasks:
                if path.suffix == ".json" and task.get("attempts", 0) >= MAX_ATTEMPTS:
                    try:
                        path.unlink()
                        cleared += 1
                    except OSError:
                        pass
        print(f"cleared {cleared} permanently-failed task(s)")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
