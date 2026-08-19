"""Agent coordination blackboard — shared state for parallel agents.

When multiple agents work in the same project simultaneously, they
need a shared "blackboard" to coordinate: claim tasks, signal
completion, leave notes for each other, and detect conflicts.

Pattern: Blackboard Architecture (from AI classical literature).
Each agent reads/writes to a shared space in the vault. No direct
agent-to-agent communication needed — coordination happens through
the shared state.

Files live at: knowledge/projects/<slug>/.blackboard/
  - tasks.jsonl     — task queue with claim/complete status
  - signals.jsonl   — inter-agent signals ("I'm working on X")
  - conflicts.jsonl — conflict detection ("agent A and B edited same file")

Usage:
    # Agent A claims a task
    uv run python scripts/blackboard.py claim --project your-project --task "implement JWT" --agent opencode

    # Agent B sees what's taken
    uv run python scripts/blackboard.py status --project your-project

    # Agent A completes
    uv run python scripts/blackboard.py complete --project your-project --task-id <id>

    # Agent B leaves a note
    uv run python scripts/blackboard.py signal --project your-project --from opencode --to codex --message "JWT done, your turn for tests"

    # Check for conflicts (two agents edited same file)
    uv run python scripts/blackboard.py conflicts --project your-project
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from markdown_transaction import (  # noqa: E402
    MAX_KNOWLEDGE_TARGET_BYTES,
    MarkdownCoordinator,
    active_markdown_coordinator,
    append_knowledge,
)
from memory_state import ROOT  # noqa: E402
from reliable_memory import begin_immediate  # noqa: E402
from secret_redact import redact_secrets  # noqa: E402

PROJECTS_DIR = ROOT / "knowledge" / "projects"
_MAX_RESOURCES = 64
_MAX_RESOURCE_BYTES = 512
_MAX_TASK_BYTES = 4096
_MAX_AGENT_BYTES = 128
_MIN_TTL_SECONDS = 1
_MAX_TTL_SECONDS = 86400


@dataclass(frozen=True)
class BlackboardClaim:
    project: str
    claim_id: str
    task: str
    agent: str
    resources: tuple[str, ...]
    lease_token: str
    resource_epochs: tuple[tuple[str, int], ...]
    heartbeat_at: datetime
    expires_at: datetime
    ttl_seconds: int


class BlackboardFenceError(RuntimeError):
    """A claim no longer owns its exact resource epochs."""


class BlackboardConflictError(RuntimeError):
    """A requested resource set overlaps a live claim."""

    def __init__(self, resources: Sequence[str], conflict_id: str) -> None:
        self.resources = tuple(resources)
        self.conflict_id = conflict_id
        super().__init__("blackboard resources are already claimed")

    def __reduce__(self):
        """Rebuild from the arguments, not from the message.

        Without this the default reduction calls `__init__` with the message
        alone, and crossing a process boundary raises a TypeError about a
        missing argument instead of delivering the failure.
        """
        return (self.__class__, (self.resources, self.conflict_id))


class _ResourceBusy(RuntimeError):
    def __init__(self, rows: Sequence[sqlite3.Row]) -> None:
        self.rows = tuple(rows)
        super().__init__("blackboard resource busy")


def _sanitize_project(project: str) -> str:
    """Allow only safe project slug segments (no path traversal)."""
    raw = (project or "").strip().replace("\\", "/").split("/")[-1]
    slug = re.sub(r"[^a-zA-Z0-9._-]", "-", raw).strip(".-")
    if not slug or slug in {".", ".."}:
        raise ValueError(f"invalid project slug: {project!r}")
    return slug


def _bb_dir(project: str) -> Path:
    slug = _sanitize_project(project)
    d = (PROJECTS_DIR / slug / ".blackboard").resolve()
    try:
        d.relative_to(PROJECTS_DIR.resolve())
    except ValueError as exc:
        raise ValueError(f"project path escapes projects dir: {project!r}") from exc
    return d


def _append_jsonl(
    path: Path, record: dict, operation_id: str | None = None
) -> None:
    block = (redact_secrets(json.dumps(record, ensure_ascii=False)) + "\n").encode("utf-8")
    append_knowledge(operation_id, path, block)


def _append_once(
    path: Path, record: dict, operation_id: str, *, key: str, value: str
) -> None:
    """Publish one record under a stable operation id, so a retry is a no-op.

    The transaction layer binds an operation id to exact bytes, and these
    records carry the moment they were written. A caller that retries after a
    transient failure would otherwise re-stamp the record and be refused, even
    though its first attempt already published the very act it is repeating.
    """
    try:
        _append_jsonl(path, record, operation_id)
    except ValueError:
        if not any(item.get(key) == value for item in _read_jsonl(path)):
            raise


def _decode_jsonl(path: Path, content: bytes) -> str:
    if len(content) > MAX_KNOWLEDGE_TARGET_BYTES:
        raise ValueError(f"{path.name} exceeds the blackboard stream limit")
    try:
        return content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path.name} is corrupt UTF-8") from exc


def _parse_jsonl_line(path: Path, line: str, line_number: int) -> dict:
    try:
        record = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} line {line_number} is corrupt") from exc
    if not isinstance(record, dict):
        raise ValueError(f"{path.name} line {line_number} is not an object")
    return record


def _parse_jsonl(path: Path, content: bytes | None) -> list[dict]:
    if content is None:
        return []
    text = _decode_jsonl(path, content)
    records: list[dict] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        records.append(_parse_jsonl_line(path, line, line_number))
    return records


def _coordinator() -> MarkdownCoordinator:
    vault = PROJECTS_DIR.resolve().parent.parent
    state_root = Path(os.environ.get("LLM_WIKI_STATE_ROOT", str(vault))).resolve()
    return active_markdown_coordinator(vault, state_root)


def _read_jsonl_snapshot(paths: tuple[Path, ...]) -> dict[Path, list[dict]]:
    coordinator = _coordinator()
    vault = PROJECTS_DIR.resolve().parent.parent
    relative_paths = tuple(path.relative_to(vault) for path in paths)
    snapshot = coordinator.coherent_read(relative_paths)
    return {
        path: _parse_jsonl(path, snapshot[relative])
        for path, relative in zip(paths, relative_paths, strict=True)
    }


def _read_jsonl(path: Path) -> list[dict]:
    return _read_jsonl_snapshot((path,))[path]


def _bounded_text(value: object, name: str, max_bytes: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    normalized = unicodedata.normalize("NFC", value)
    if len(normalized.encode("utf-8")) > max_bytes:
        raise ValueError(f"{name} exceeds its size limit")
    return normalized


def _require_relative_resource(value: str) -> None:
    if value.startswith("/") or re.match(r"^[a-zA-Z]:", value):
        raise ValueError("blackboard resource must be relative")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("blackboard resource contains traversal or empty segments")


def _require_safe_resource_characters(value: str) -> None:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("blackboard resource contains control characters")
    if len(value.encode("utf-8")) > _MAX_RESOURCE_BYTES:
        raise ValueError("blackboard resource exceeds its size limit")


def _normalize_resource(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("blackboard resource must be a non-empty string")
    slashed = unicodedata.normalize("NFC", value).replace("\\", "/")
    _require_relative_resource(slashed)
    _require_safe_resource_characters(slashed)
    return unicodedata.normalize("NFC", slashed.casefold())


def _require_resource_sequence(resources: object) -> Sequence[str]:
    if isinstance(resources, (str, bytes)):
        raise TypeError("blackboard resources must be an array")
    if not isinstance(resources, Sequence):
        raise TypeError("blackboard resources must be an array")
    return resources


def _normalize_resources(resources: Sequence[str]) -> tuple[str, ...]:
    resources = _require_resource_sequence(resources)
    if not 1 <= len(resources) <= _MAX_RESOURCES:
        raise ValueError("blackboard resource set must be bounded and non-empty")
    normalized = tuple(sorted(_normalize_resource(item) for item in resources))
    if len(normalized) != len(set(normalized)):
        raise ValueError("blackboard resource set contains duplicates")
    return normalized


def _require_ttl(ttl_seconds: int) -> int:
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise TypeError("blackboard ttl_seconds must be an integer")
    if not _MIN_TTL_SECONDS <= ttl_seconds <= _MAX_TTL_SECONDS:
        raise ValueError("blackboard ttl_seconds is outside its bounds")
    return ttl_seconds


def _utc_now(value: datetime | None) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise ValueError("blackboard time must be timezone-aware")
    return current.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("blackboard timestamp is invalid")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _claim_rows(
    database: sqlite3.Connection, project: str, claim_id: str
) -> list[sqlite3.Row]:
    return list(
        database.execute(
            "SELECT * FROM blackboard_claims WHERE project=? AND claim_id=? "
            "ORDER BY resource",
            (project, claim_id),
        )
    )


def _busy_claim_rows(
    database: sqlite3.Connection, project: str, resources: tuple[str, ...]
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _resource in resources)
    return list(
        database.execute(
            "SELECT * FROM blackboard_claims WHERE project=? "
            f"AND resource IN ({placeholders}) ORDER BY resource",
            (project, *resources),
        )
    )


def _next_claim_epoch(
    database: sqlite3.Connection, project: str, resource: str
) -> int:
    row = database.execute(
        "SELECT last_epoch FROM blackboard_claim_epochs WHERE project=? AND resource=?",
        (project, resource),
    ).fetchone()
    next_epoch = 1
    if row is not None:
        next_epoch = int(row["last_epoch"]) + 1
    database.execute(
        "INSERT INTO blackboard_claim_epochs(project,resource,last_epoch) VALUES(?,?,?) "
        "ON CONFLICT(project,resource) DO UPDATE SET last_epoch=excluded.last_epoch",
        (project, resource, next_epoch),
    )
    return next_epoch


def _insert_claim_resource(
    database: sqlite3.Connection,
    claim: BlackboardClaim,
    resource: str,
    epoch: int,
) -> None:
    database.execute(
        "INSERT INTO blackboard_claims("
        "project,resource,claim_id,agent,lease_token,fencing_epoch,heartbeat_at,expires_at"
        ") VALUES(?,?,?,?,?,?,?,?)",
        (
            claim.project,
            resource,
            claim.claim_id,
            claim.agent,
            claim.lease_token,
            epoch,
            _timestamp(claim.heartbeat_at),
            _timestamp(claim.expires_at),
        ),
    )


def _acquire_resources(
    database: sqlite3.Connection, claim: BlackboardClaim
) -> tuple[tuple[str, int], ...]:
    database.execute(
        "DELETE FROM blackboard_claims WHERE project=? AND expires_at<=?",
        (claim.project, _timestamp(claim.heartbeat_at)),
    )
    busy = _busy_claim_rows(database, claim.project, claim.resources)
    if busy:
        raise _ResourceBusy(busy)
    epochs: list[tuple[str, int]] = []
    for resource in claim.resources:
        epoch = _next_claim_epoch(database, claim.project, resource)
        _insert_claim_resource(database, claim, resource, epoch)
        epochs.append((resource, epoch))
    return tuple(epochs)


def _claim_request_record(claim: BlackboardClaim) -> dict[str, object]:
    return {
        "kind": "claim-request",
        "id": claim.claim_id,
        "claim_id": claim.claim_id,
        "project": claim.project,
        "task": claim.task,
        "agent": claim.agent,
        "resources": list(claim.resources),
        "lease_token_sha256": hashlib.sha256(claim.lease_token.encode()).hexdigest(),
        "status": "requesting",
        "requested_at": _timestamp(claim.heartbeat_at),
        "ttl_seconds": claim.ttl_seconds,
    }


def _claim_active_record(claim: BlackboardClaim) -> dict[str, object]:
    return {
        "kind": "claim-activated",
        "id": claim.claim_id,
        "claim_id": claim.claim_id,
        "status": "claimed",
        "claimed_at": _timestamp(claim.heartbeat_at),
        "completed_at": None,
        "expires_at": _timestamp(claim.expires_at),
        "resource_epochs": [list(item) for item in claim.resource_epochs],
    }


def _conflict_record(
    claim: BlackboardClaim, busy: _ResourceBusy, conflict_id: str
) -> dict[str, object]:
    resources = sorted({str(row["resource"]) for row in busy.rows})
    holders = sorted(
        {
            (str(row["resource"]), str(row["claim_id"]), str(row["agent"]))
            for row in busy.rows
        }
    )
    return {
        "kind": "conflict",
        "conflict_id": conflict_id,
        "project": claim.project,
        "requested_claim_id": claim.claim_id,
        "requested_agent": claim.agent,
        "requested_task": claim.task,
        "resources": resources,
        "holders": [
            {"resource": resource, "claim_id": claim_id, "agent": agent}
            for resource, claim_id, agent in holders
        ],
        "at": _timestamp(claim.heartbeat_at),
    }


def _raise_conflict(project: str, claim: BlackboardClaim, busy: _ResourceBusy) -> None:
    conflict_id = secrets.token_hex(32)
    record = _conflict_record(claim, busy, conflict_id)
    _append_jsonl(
        _bb_dir(project) / "conflicts.jsonl",
        record,
        f"blackboard-conflict:{conflict_id}",
    )
    raise BlackboardConflictError(record["resources"], conflict_id)


def _new_claim(
    project: str,
    task: str,
    agent: str,
    resources: tuple[str, ...],
    ttl_seconds: int,
    now: datetime,
) -> BlackboardClaim:
    return BlackboardClaim(
        project=project,
        claim_id=secrets.token_hex(32),
        task=task,
        agent=agent,
        resources=resources,
        lease_token=secrets.token_hex(32),
        resource_epochs=(),
        heartbeat_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
        ttl_seconds=ttl_seconds,
    )


def _acquire_claim(coordinator: MarkdownCoordinator, claim: BlackboardClaim) -> BlackboardClaim:
    with coordinator._connect() as database, begin_immediate(database):
        epochs = _acquire_resources(database, claim)
    return replace(claim, resource_epochs=epochs)


def claim_task(
    project: str,
    task: str,
    agent: str,
    *,
    resources: Sequence[str],
    ttl_seconds: int = 30,
    now: datetime | None = None,
) -> BlackboardClaim:
    """Atomically claim a bounded resource set and return its fencing lease."""
    slug = _sanitize_project(project)
    current = _utc_now(now)
    claim = _new_claim(
        slug,
        _bounded_text(task, "task", _MAX_TASK_BYTES),
        _bounded_text(agent, "agent", _MAX_AGENT_BYTES),
        _normalize_resources(resources),
        _require_ttl(ttl_seconds),
        current,
    )
    tasks_file = _bb_dir(slug) / "tasks.jsonl"
    _append_jsonl(tasks_file, _claim_request_record(claim), f"blackboard-request:{claim.claim_id}")
    try:
        acquired = _acquire_claim(_coordinator(), claim)
    except _ResourceBusy as busy:
        _raise_conflict(slug, claim, busy)
    _append_jsonl(tasks_file, _claim_active_record(acquired), f"blackboard-active:{claim.claim_id}")
    return acquired


def _row_epochs(rows: Sequence[sqlite3.Row]) -> tuple[tuple[str, int], ...]:
    return tuple((str(row["resource"]), int(row["fencing_epoch"])) for row in rows)


def _require_claim_identity(rows: Sequence[sqlite3.Row], claim: BlackboardClaim) -> None:
    if _row_epochs(rows) != claim.resource_epochs:
        raise BlackboardFenceError("blackboard claim resource epochs changed")
    if any(row["lease_token"] != claim.lease_token for row in rows):
        raise BlackboardFenceError("blackboard claim lease token changed")


def _require_live_claim_rows(
    rows: Sequence[sqlite3.Row], claim: BlackboardClaim, now: datetime
) -> None:
    _require_claim_identity(rows, claim)
    if any(_parse_timestamp(row["expires_at"]) <= now for row in rows):
        raise BlackboardFenceError("blackboard claim lease expired")


def _load_live_claim(
    coordinator: MarkdownCoordinator, claim: BlackboardClaim, now: datetime
) -> None:
    with coordinator._connect() as database, begin_immediate(database):
        _require_live_claim_rows(_claim_rows(database, claim.project, claim.claim_id), claim, now)


def _heartbeat_rows(
    database: sqlite3.Connection,
    claim: BlackboardClaim,
    now: datetime,
    expires_at: datetime,
) -> None:
    rows = _claim_rows(database, claim.project, claim.claim_id)
    _require_live_claim_rows(rows, claim, now)
    changed = database.execute(
        "UPDATE blackboard_claims SET heartbeat_at=?,expires_at=? "
        "WHERE project=? AND claim_id=? AND lease_token=?",
        (
            _timestamp(now),
            _timestamp(expires_at),
            claim.project,
            claim.claim_id,
            claim.lease_token,
        ),
    ).rowcount
    if changed != len(claim.resources):
        raise BlackboardFenceError("blackboard heartbeat lost its complete resource set")


def heartbeat_claim(
    claim: BlackboardClaim, *, now: datetime | None = None
) -> BlackboardClaim:
    """Renew one exact live claim without reviving an expired epoch."""
    if not isinstance(claim, BlackboardClaim):
        raise TypeError("claim must be a BlackboardClaim")
    current = _utc_now(now)
    expires_at = current + timedelta(seconds=claim.ttl_seconds)
    coordinator = _coordinator()
    with coordinator._connect() as database, begin_immediate(database):
        _heartbeat_rows(database, claim, current, expires_at)
    return replace(claim, heartbeat_at=current, expires_at=expires_at)


def _completion_record(claim: BlackboardClaim, now: datetime) -> dict[str, object]:
    return {
        "kind": "completion",
        "id": claim.claim_id,
        "claim_id": claim.claim_id,
        "project": claim.project,
        "resources": list(claim.resources),
        "resource_epochs": [list(item) for item in claim.resource_epochs],
        "lease_token_sha256": hashlib.sha256(claim.lease_token.encode()).hexdigest(),
        "completed_at": _timestamp(now),
    }


def _delete_exact_claim(
    coordinator: MarkdownCoordinator, claim: BlackboardClaim
) -> bool:
    with coordinator._connect() as database, begin_immediate(database):
        rows = _claim_rows(database, claim.project, claim.claim_id)
        if not rows:
            return False
        _require_claim_identity(rows, claim)
        changed = database.execute(
            "DELETE FROM blackboard_claims WHERE project=? AND claim_id=? AND lease_token=?",
            (claim.project, claim.claim_id, claim.lease_token),
        ).rowcount
        if changed != len(claim.resources):
            raise BlackboardFenceError("blackboard release lost its complete resource set")
    return True


def complete_task(project: str, claim: BlackboardClaim) -> bool:
    """Publish completion, then release only the exact claim epochs."""
    if not isinstance(claim, BlackboardClaim):
        raise TypeError("complete_task requires a BlackboardClaim")
    slug = _sanitize_project(project)
    if slug != claim.project:
        raise BlackboardFenceError("blackboard claim project changed")
    current = _utc_now(None)
    coordinator = _coordinator()
    _load_live_claim(coordinator, claim, current)
    _append_once(
        _bb_dir(slug) / "completed.jsonl",
        _completion_record(claim, current),
        f"blackboard-complete:{claim.claim_id}",
        key="id",
        value=claim.claim_id,
    )
    _delete_exact_claim(coordinator, claim)
    return True


def _request_matches_rows(record: dict, rows: Sequence[sqlite3.Row]) -> bool:
    resources = tuple(str(row["resource"]) for row in rows)
    if resources != tuple(record.get("resources", ())):
        return False
    token_hashes = {
        hashlib.sha256(str(row["lease_token"]).encode()).hexdigest() for row in rows
    }
    return token_hashes == {record.get("lease_token_sha256")}


def _activation_from_rows(record: dict, rows: Sequence[sqlite3.Row]) -> dict[str, object]:
    return {
        "kind": "claim-activated",
        "id": record["claim_id"],
        "claim_id": record["claim_id"],
        "status": "claimed",
        "claimed_at": rows[0]["heartbeat_at"],
        "completed_at": None,
        "expires_at": rows[0]["expires_at"],
        "resource_epochs": [
            [str(row["resource"]), int(row["fencing_epoch"])] for row in rows
        ],
    }


def _activate_request(
    coordinator: MarkdownCoordinator, tasks_file: Path, record: dict
) -> bool:
    with coordinator._connect() as database:
        rows = _claim_rows(database, str(record["project"]), str(record["claim_id"]))
    if not rows or not _request_matches_rows(record, rows):
        return False
    active = _activation_from_rows(record, rows)
    _append_jsonl(tasks_file, active, f"blackboard-active:{record['claim_id']}")
    return True


def _activate_pending_requests(
    coordinator: MarkdownCoordinator, tasks_file: Path, records: Sequence[dict]
) -> bool:
    activated_records = _records_of_kind(records, "claim-activated")
    activated = {record.get("claim_id") for record in activated_records}
    requests = _records_of_kind(records, "claim-request")
    changed = False
    for record in requests:
        if record.get("claim_id") in activated:
            continue
        changed = _activate_request(coordinator, tasks_file, record) or changed
    return changed


def _completion_resources_match(record: dict, rows: Sequence[sqlite3.Row]) -> bool:
    resources = tuple(str(row["resource"]) for row in rows)
    return resources == tuple(record.get("resources", ()))


def _completion_matches_rows(record: dict, rows: Sequence[sqlite3.Row]) -> bool:
    if not _completion_resources_match(record, rows):
        return False
    epochs = tuple((str(item[0]), int(item[1])) for item in record["resource_epochs"])
    if _row_epochs(rows) != epochs:
        return False
    token_hashes = {
        hashlib.sha256(str(row["lease_token"]).encode()).hexdigest() for row in rows
    }
    return token_hashes == {record.get("lease_token_sha256")}


def _records_of_kind(records: Sequence[dict], kind: str) -> list[dict]:
    return [record for record in records if record.get("kind") == kind]


def _release_completion(
    coordinator: MarkdownCoordinator, project: str, record: dict
) -> bool:
    claim_id = str(record["claim_id"])
    with coordinator._connect() as database, begin_immediate(database):
        rows = _claim_rows(database, project, claim_id)
        if not rows or not _completion_matches_rows(record, rows):
            return False
        database.execute(
            "DELETE FROM blackboard_claims WHERE project=? AND claim_id=?",
            (project, claim_id),
        )
    return True


def _release_completed_claims(
    coordinator: MarkdownCoordinator, project: str, records: Sequence[dict]
) -> bool:
    completions = _records_of_kind(records, "completion")
    changed = False
    for record in completions:
        changed = _release_completion(coordinator, project, record) or changed
    return changed


def _reconcile_snapshot(
    project: str, paths: tuple[Path, ...], snapshot: dict[Path, list[dict]]
) -> bool:
    coordinator = _coordinator()
    activated = _activate_pending_requests(coordinator, paths[0], snapshot[paths[0]])
    released = _release_completed_claims(coordinator, project, snapshot[paths[1]])
    return activated or released


def _fold_task_record(tasks: dict[str, dict], record: dict) -> None:
    task_id = record.get("id")
    if not isinstance(task_id, str):
        return
    if record.get("kind") in {None, "claim-request"}:
        tasks[task_id] = dict(record)
        return
    if record.get("kind") == "claim-activated" and task_id in tasks:
        tasks[task_id].update(record)


def _fold_tasks(records: Sequence[dict]) -> list[dict]:
    tasks: dict[str, dict] = {}
    for record in records:
        _fold_task_record(tasks, record)
    return [task for task in tasks.values() if task.get("status") == "claimed"]


def _partition_tasks(
    tasks: Sequence[dict], completed_ids: set[object]
) -> tuple[list[dict], list[dict]]:
    completed = [task for task in tasks if task.get("id") in completed_ids]
    active = [task for task in tasks if task.get("id") not in completed_ids]
    return active, completed


def get_status(project: str) -> dict:
    """Return one coherent folded view of immutable blackboard streams."""
    slug = _sanitize_project(project)
    bb = _bb_dir(slug)
    paths = tuple(bb / name for name in ("tasks.jsonl", "completed.jsonl", "signals.jsonl"))
    snapshot = _read_jsonl_snapshot(paths)
    if _reconcile_snapshot(slug, paths, snapshot):
        snapshot = _read_jsonl_snapshot(paths)
    tasks = _fold_tasks(snapshot[paths[0]])
    completed_ids = {record.get("id") for record in snapshot[paths[1]]}
    active, completed = _partition_tasks(tasks, completed_ids)
    return {
        "project": slug,
        "active_tasks": len(active),
        "completed_tasks": len(completed),
        "active_agents": sorted({str(task["agent"]) for task in active}),
        "recent_signals": snapshot[paths[2]][-5:],
        "tasks": active[:10],
    }


def send_signal(project: str, from_agent: str, to_agent: str, message: str) -> None:
    """Leave a signal for another agent."""
    slug = _sanitize_project(project)
    signals_file = _bb_dir(slug) / "signals.jsonl"
    record = {
        "from": _bounded_text(from_agent, "from_agent", _MAX_AGENT_BYTES),
        "to": _bounded_text(to_agent, "to_agent", _MAX_AGENT_BYTES),
        "message": _bounded_text(message, "message", _MAX_TASK_BYTES),
        "at": _timestamp(_utc_now(None)),
    }
    _append_jsonl(signals_file, record)


def _fold_conflict_record(active: dict[str, dict], record: dict) -> None:
    conflict_id = record.get("conflict_id")
    if not isinstance(conflict_id, str):
        return
    if record.get("kind") == "conflict":
        active[conflict_id] = record
        return
    if record.get("kind") == "resolution":
        active.pop(conflict_id, None)


def detect_conflicts(project: str) -> list[dict]:
    """Return unresolved immutable resource-conflict events."""
    records = _read_jsonl(_bb_dir(project) / "conflicts.jsonl")
    active: dict[str, dict] = {}
    for record in records:
        _fold_conflict_record(active, record)
    return list(active.values())


def resolve_conflict(
    project: str, conflict_id: str, *, agent: str, resolution: str
) -> None:
    slug = _sanitize_project(project)
    identity = _bounded_text(conflict_id, "conflict_id", 128)
    if identity not in {record["conflict_id"] for record in detect_conflicts(slug)}:
        raise KeyError(identity)
    record = {
        "kind": "resolution",
        "conflict_id": identity,
        "agent": _bounded_text(agent, "agent", _MAX_AGENT_BYTES),
        "resolution": _bounded_text(resolution, "resolution", _MAX_TASK_BYTES),
        "at": _timestamp(_utc_now(None)),
    }
    _append_once(
        _bb_dir(slug) / "conflicts.jsonl",
        record,
        f"blackboard-resolution:{identity}",
        key="conflict_id",
        value=identity,
    )


def _claim_payload(claim: BlackboardClaim) -> dict[str, object]:
    payload = asdict(claim)
    payload["heartbeat_at"] = _timestamp(claim.heartbeat_at)
    payload["expires_at"] = _timestamp(claim.expires_at)
    payload["resources"] = list(claim.resources)
    payload["resource_epochs"] = [list(item) for item in claim.resource_epochs]
    return payload


def _require_hex(value: object, name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{name} must be lowercase 64-hex")
    return value


def _claim_from_payload(payload: object) -> BlackboardClaim:
    if not isinstance(payload, dict):
        raise ValueError("blackboard claim JSON must be an object")
    resources = _normalize_resources(payload["resources"])
    epochs = tuple(
        (_normalize_resource(item[0]), int(item[1]))
        for item in _require_resource_sequence(payload["resource_epochs"])
    )
    if tuple(resource for resource, _epoch in epochs) != resources:
        raise ValueError("blackboard claim epochs do not match resources")
    return BlackboardClaim(
        project=_sanitize_project(str(payload["project"])),
        claim_id=_require_hex(payload["claim_id"], "claim_id"),
        task=_bounded_text(payload["task"], "task", _MAX_TASK_BYTES),
        agent=_bounded_text(payload["agent"], "agent", _MAX_AGENT_BYTES),
        resources=resources,
        lease_token=_require_hex(payload["lease_token"], "lease_token"),
        resource_epochs=epochs,
        heartbeat_at=_parse_timestamp(payload["heartbeat_at"]),
        expires_at=_parse_timestamp(payload["expires_at"]),
        ttl_seconds=_require_ttl(int(payload["ttl_seconds"])),
    )


def _claim_from_json(value: str) -> BlackboardClaim:
    raw = sys.stdin.read() if value == "-" else value
    if len(raw.encode("utf-8")) > 16384:
        raise ValueError("blackboard claim JSON exceeds its size limit")
    try:
        return _claim_from_payload(json.loads(raw))
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("blackboard claim JSON is invalid") from exc


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _cli_claim(args: argparse.Namespace) -> int:
    claim = claim_task(
        args.project,
        args.task,
        args.agent,
        resources=args.resource,
        ttl_seconds=args.ttl_seconds,
    )
    _print_json(_claim_payload(claim))
    return 0


def _cli_complete(args: argparse.Namespace) -> int:
    claim = _claim_from_json(args.claim_json)
    _print_json({"completed": complete_task(args.project, claim), "id": claim.claim_id})
    return 0


def _cli_heartbeat(args: argparse.Namespace) -> int:
    renewed = heartbeat_claim(_claim_from_json(args.claim_json))
    _print_json(_claim_payload(renewed))
    return 0


def _cli_status(args: argparse.Namespace) -> int:
    _print_json(get_status(args.project))
    return 0


def _cli_signal(args: argparse.Namespace) -> int:
    send_signal(args.project, args.from_agent, args.to, args.message)
    _print_json({"sent": True})
    return 0


def _cli_conflicts(args: argparse.Namespace) -> int:
    _print_json(detect_conflicts(args.project))
    return 0


def _cli_resolve(args: argparse.Namespace) -> int:
    resolve_conflict(
        args.project,
        args.conflict_id,
        agent=args.agent,
        resolution=args.resolution,
    )
    _print_json({"resolved": args.conflict_id})
    return 0


def _claim_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("claim", help="Claim a resource set")
    parser.add_argument("--project", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--resource", action="append", required=True)
    parser.add_argument("--ttl-seconds", type=int, default=30)
    parser.set_defaults(handler=_cli_claim)


def _lease_parser(
    subparsers: argparse._SubParsersAction, command: str, handler: object
) -> None:
    parser = subparsers.add_parser(command, help=f"{command.title()} a claim")
    parser.add_argument("--project", required=command == "complete")
    parser.add_argument("--claim-json", required=True, help="Claim JSON or '-' for stdin")
    parser.set_defaults(handler=handler)


def _simple_project_parser(
    subparsers: argparse._SubParsersAction, command: str, handler: object
) -> None:
    parser = subparsers.add_parser(command)
    parser.add_argument("--project", required=True)
    parser.set_defaults(handler=handler)


def _signal_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("signal", help="Send an agent signal")
    parser.add_argument("--project", required=True)
    parser.add_argument("--from", dest="from_agent", required=True)
    parser.add_argument("--to", required=True)
    parser.add_argument("--message", required=True)
    parser.set_defaults(handler=_cli_signal)


def _resolve_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("resolve", help="Resolve a conflict event")
    parser.add_argument("--project", required=True)
    parser.add_argument("--conflict-id", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--resolution", required=True)
    parser.set_defaults(handler=_cli_resolve)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent coordination blackboard.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _claim_parser(subparsers)
    _lease_parser(subparsers, "complete", _cli_complete)
    _lease_parser(subparsers, "heartbeat", _cli_heartbeat)
    _simple_project_parser(subparsers, "status", _cli_status)
    _signal_parser(subparsers)
    _simple_project_parser(subparsers, "conflicts", _cli_conflicts)
    _resolve_parser(subparsers)
    return parser


def main() -> int:
    args = _parser().parse_args()
    handler = args.handler
    if not callable(handler):
        raise RuntimeError("blackboard command handler is invalid")
    return int(handler(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
