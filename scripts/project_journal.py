"""Fenced append-only project journals and deterministic state projections."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from markdown_transaction import (
    ABSENT,
    MarkdownChange,
    MarkdownCoordinator,
    ProjectCheckpointReservation,
    ProjectPendingPriorError,
    TransactionFailure,
)
from reliable_memory import (
    begin_immediate,
    canonical_json_bytes,
    sha256_bytes,
    validate_schema,
)

JOURNAL_HEADER = """---
type: project-journal
schema_version: project-checkpoint/v1
---
# Project Journal

<!-- Append-only canonical JSON checkpoint events follow. -->
"""

_SCHEMA = Path(__file__).with_name("schemas") / "project-checkpoint-v1.json"
_HEARTBEAT_SECONDS = 10
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_PROJECTION_BYTES = 1024 * 1024
MAX_JOURNAL_EVENTS = 1000
SESSION_START_RECOVERY_SECONDS = 0.25
MAX_PROJECT_HANDOFF_CHARS = 2400
_MAX_VALUE_CHARS = 240
_MAX_LIST_ITEMS = {
    "next_actions": 5,
    "decisions": 5,
    "blockers": 5,
    "changed_files": 10,
    "commands": 5,
    "verification": 5,
}


class ProjectFenceError(RuntimeError):
    """The caller no longer owns the current unexpired project lease."""


class ProjectLeaseBusy(ProjectFenceError):
    """Another owner holds the current project lease."""


class ProjectJournalRebuildRequired(RuntimeError):
    """Append-only journal ordering requires an explicit verified rebuild."""

    status = "journal_rebuild_required"

    def __init__(self, project: str, sequence: int, journal_head: int):
        super().__init__(
            f"project {project!r} sequence {sequence} follows journal sequence {journal_head}"
        )
        self.project = project
        self.sequence = sequence
        self.journal_head = journal_head


class ProjectJournalReadError(RuntimeError):
    """A project file cannot be safely read as a bounded regular file."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.status = code


@dataclass(frozen=True)
class ProjectLease:
    slug: str
    owner: str
    token: str
    epoch: int
    expires_at: datetime
    heartbeat_at: datetime

    @property
    def heartbeat_due_at(self) -> datetime:
        return self.heartbeat_at + timedelta(seconds=_HEARTBEAT_SECONDS)


@dataclass(frozen=True)
class CheckpointReceipt:
    project: str
    sequence: int
    occurrence_id: str
    idempotency_key: str
    transaction_id: str | None
    duplicate: bool = False


@dataclass(frozen=True)
class CheckpointDecision:
    reason: str
    forced: bool = False
    maintenance: bool = False
    checkpoint_at: datetime | None = None
    dirty_threshold: int | None = None
    next_token_threshold: int | None = None


@dataclass
class ProjectProjection:
    project: str
    goal: dict[str, str] = field(default_factory=dict)
    phase: dict[str, str] = field(default_factory=dict)
    current_task: dict[str, str] = field(default_factory=dict)
    next_actions: dict[str, str] = field(default_factory=dict)
    decisions: dict[str, str] = field(default_factory=dict)
    blockers: dict[str, str] = field(default_factory=dict)
    changed_files: dict[str, str] = field(default_factory=dict)
    commands: dict[str, str] = field(default_factory=dict)
    verification: dict[str, str] = field(default_factory=dict)
    legacy_context: str = ""
    last_applied_sequence: int = 0


@dataclass(frozen=True)
class ProjectHandoffResult:
    context: str
    degraded: bool = False
    legacy: bool = False


class CheckpointReducer:
    """Reduce observed lifecycle signals into deterministic checkpoint decisions."""

    _BYPASS_TYPES = frozenset(
        {
            "pre_compact",
            "compaction_confirmed",
            "decision",
            "task_completed",
            "task_cancelled",
            "significant_failure",
            "ownership_transferred",
            "session_end",
        }
    )
    _SIGNIFICANT_TYPES = frozenset(
        {
            "decision",
            "correction",
            "blocker_opened",
            "blocker_closed",
            "task_completed",
            "task_cancelled",
            "ownership_transferred",
            "significant_failure",
            "failed_command",
            "mutation",
            "file_changed",
            "public_contract_changed",
            "test_result_changed",
        }
    )
    _REASONS = {
        "decision": "decision",
        "correction": "correction",
        "blocker_opened": "blocker_change",
        "blocker_closed": "blocker_change",
        "task_completed": "task_completed",
        "task_cancelled": "task_cancelled",
        "ownership_transferred": "ownership_transfer",
        "significant_failure": "significant_failure",
        "failed_command": "significant_failure",
        "public_contract_changed": "public_contract_change",
        "test_result_changed": "test_result_change",
    }

    def __init__(
        self,
        *,
        host_progress_signals: bool = False,
        significant_count: int = 0,
        token_threshold: int = 60,
        dirty_since: datetime | None = None,
        dirty_thresholds: Sequence[int] = (),
        last_checkpoint_at: datetime | None = None,
        observed_event_ids: Sequence[str] = (),
    ):
        self.host_progress_signals = bool(host_progress_signals)
        self.significant_count = max(0, int(significant_count))
        self.token_threshold = max(60, int(token_threshold))
        self.dirty_since = dirty_since
        self.dirty_thresholds = {int(value) for value in dirty_thresholds}
        self.last_checkpoint_at = last_checkpoint_at
        self.observed_event_ids = dict.fromkeys(str(value) for value in observed_event_ids)

    def observe(
        self,
        event: Mapping[str, object],
        *,
        now: datetime | None = None,
        commit: bool = True,
    ) -> CheckpointDecision | None:
        if not isinstance(event, Mapping):
            raise TypeError("checkpoint observation must be a mapping")
        current_time = now or _utc_now()
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise ValueError("checkpoint observation time must be timezone-aware")
        current_time = current_time.astimezone(timezone.utc)
        event_id = event.get("event_id")
        if isinstance(event_id, str) and event_id:
            if event_id in self.observed_event_ids:
                return None
            self.observed_event_ids[event_id] = None
            while len(self.observed_event_ids) > 256:
                self.observed_event_ids.pop(next(iter(self.observed_event_ids)))

        event_type = str(event.get("type") or "")
        dirty = event.get("dirty")
        if dirty is True and self.dirty_since is None:
            self.dirty_since = current_time
        elif dirty is False:
            self.dirty_since = None
            self.dirty_thresholds.clear()
        dirty_active = dirty is True or dirty is None and self.dirty_since is not None

        significant = event_type in self._SIGNIFICANT_TYPES
        if event_type in {"mutation", "file_changed"}:
            significant = event.get("changed") is True or event.get("significant") is True
        if significant:
            self.significant_count += 1

        reason: str | None = None
        forced = False
        pending_dirty_threshold: int | None = None
        pending_token_threshold: int | None = None
        if event_type == "session_start":
            reason = "session_start_recovery"
        elif event_type == "pre_compact":
            reason = "before_compaction"
        elif event_type == "compaction_confirmed":
            self.host_progress_signals = True
            reason = "after_compaction"
        elif event_type == "token_usage":
            self.host_progress_signals = True
            percent = event.get("percent")
            if isinstance(percent, (int, float)) and not isinstance(percent, bool):
                bounded = min(int(percent), 80)
                if bounded >= self.token_threshold:
                    threshold = 80 if bounded >= 80 else self.token_threshold
                    reason = f"token_{threshold}"
                    pending_token_threshold = threshold
                    if threshold == 80:
                        reason = "token_forced_80"
                        forced = True
        elif event_type == "session_end":
            reason = "session_end"
        elif event_type == "stop" and dirty_active:
            reason = "dirty_stop"
        elif event_type == "session_idle" and dirty_active:
            reason = "dirty_idle"
        elif event_type in {"file_changed", "mutation"} and event.get("significant") is True:
            reason = "file_change"
        else:
            reason = self._REASONS.get(event_type)

        if reason is None and self.dirty_since is not None:
            elapsed = current_time - self.dirty_since
            for minutes in (10, 30):
                if elapsed >= timedelta(minutes=minutes) and minutes not in self.dirty_thresholds:
                    pending_dirty_threshold = minutes
                    reason = f"dirty_{minutes}_minutes"
                    break

        if (
            reason is None
            and significant
            and not self.host_progress_signals
            and self.significant_count % 20 == 0
        ):
            reason = f"significant_event_{self.significant_count}"
        if reason is None:
            return None

        bypass = event_type in self._BYPASS_TYPES or forced
        if (
            not bypass
            and self.last_checkpoint_at is not None
            and current_time - self.last_checkpoint_at < timedelta(seconds=30)
        ):
            return None
        decision = CheckpointDecision(
            reason,
            forced,
            maintenance=reason == "session_start_recovery",
            checkpoint_at=current_time,
            dirty_threshold=pending_dirty_threshold,
            next_token_threshold=(
                pending_token_threshold + 10
                if pending_token_threshold is not None
                else None
            ),
        )
        if commit:
            self.commit_observation(
                decision,
                outcome="maintenance" if decision.maintenance else "checkpoint",
            )
        return decision

    def commit_observation(
        self,
        decision: CheckpointDecision | None,
        *,
        outcome: str,
    ) -> None:
        """Finalize reducer state after the observation's required action succeeds."""
        expected = (
            "no_checkpoint"
            if decision is None
            else "maintenance"
            if decision.maintenance
            else "checkpoint"
        )
        if outcome != expected:
            raise ValueError(f"expected {expected} observation outcome")
        if decision is None or decision.maintenance:
            return
        if decision.dirty_threshold is not None:
            self.dirty_thresholds.add(decision.dirty_threshold)
        if decision.next_token_threshold is not None:
            self.token_threshold = decision.next_token_threshold
        self.last_checkpoint_at = decision.checkpoint_at

    def to_state(self) -> dict[str, object]:
        return {
            "progress_signal_observed": self.host_progress_signals,
            "significant_count": self.significant_count,
            "token_threshold": self.token_threshold,
            "dirty_since": _timestamp(self.dirty_since) if self.dirty_since else None,
            "dirty_thresholds": sorted(self.dirty_thresholds),
            "last_checkpoint_at": (
                _timestamp(self.last_checkpoint_at) if self.last_checkpoint_at else None
            ),
            "observed_event_ids": list(self.observed_event_ids),
        }

    @classmethod
    def from_state(cls, state: Mapping[str, object] | None) -> CheckpointReducer:
        value = state if isinstance(state, Mapping) else {}
        dirty_since = value.get("dirty_since")
        last_checkpoint_at = value.get("last_checkpoint_at")
        thresholds = value.get("dirty_thresholds")
        event_ids = value.get("observed_event_ids")
        return cls(
            host_progress_signals=value.get("progress_signal_observed") is True,
            significant_count=int(value.get("significant_count") or 0),
            token_threshold=int(value.get("token_threshold") or 60),
            dirty_since=(
                _parse_timestamp(dirty_since) if isinstance(dirty_since, str) else None
            ),
            dirty_thresholds=(
                [int(item) for item in thresholds]
                if isinstance(thresholds, list)
                else ()
            ),
            last_checkpoint_at=(
                _parse_timestamp(last_checkpoint_at)
                if isinstance(last_checkpoint_at, str)
                else None
            ),
            observed_event_ids=(
                [str(item) for item in event_ids]
                if isinstance(event_ids, list)
                else ()
            ),
        )


def build_handoff(
    project: ProjectProjection,
    *,
    max_actions: int = 3,
    max_chars: int = 2400,
) -> str:
    """Render the bounded operational subset used for SessionStart handoff."""
    if not isinstance(project, ProjectProjection):
        raise TypeError("project must be a ProjectProjection")
    if max_actions < 0 or max_chars < 1:
        raise ValueError("handoff bounds must be positive")
    max_actions = min(max_actions, 3)

    lines = [f"# Project handoff: {project.project}"]

    def add_section(title: str, values: Sequence[tuple[str, str]]) -> None:
        if not values:
            return
        lines.extend(("", f"## {title}"))
        lines.extend(f"- `{item_id}`: {value}" for item_id, value in values)

    add_section("Active goal", list(project.goal.items())[-1:])
    add_section("Active task", list(project.current_task.items())[-1:])
    add_section("Next actions", list(project.next_actions.items())[-max_actions:])
    add_section("Blockers", list(project.blockers.items()))
    add_section("Recent decisions", list(project.decisions.items())[-5:])
    if project.legacy_context:
        lines.extend(("", "## Legacy context", project.legacy_context))
    identifiers = "\n".join(
        (
            "## MCP identifiers",
            f"- `project:{project.project}`",
            f"- `sequence:{project.last_applied_sequence}`",
        )
    )
    body = "\n".join(lines).rstrip()
    text = body + "\n\n" + identifiers + "\n"
    if len(text) <= max_chars:
        return text
    suffix = "\n... (handoff truncated)\n\n" + identifiers + "\n"
    if len(suffix) >= max_chars:
        return suffix[-max_chars:]
    return body[: max_chars - len(suffix)].rstrip() + suffix


def recover_project_handoff(
    store: ProjectStore,
    slug: str,
    *,
    writer_wait_seconds: float = SESSION_START_RECOVERY_SECONDS,
    max_chars: int = MAX_PROJECT_HANDOFF_CHARS,
    project_root: Path | str | None = None,
) -> ProjectHandoffResult:
    """Recover briefly, then render the last committed bounded project handoff."""
    degraded = False
    try:
        store.recover(slug, writer_wait_seconds=writer_wait_seconds)
    except TimeoutError:
        degraded = True
    projection = store.projection(slug)
    legacy = False
    if project_root is not None and projection.last_applied_sequence == 0:
        candidate = store.legacy_projection(slug, project_root)
        if candidate is not None:
            projection = candidate
            legacy = True
    if not degraded:
        return ProjectHandoffResult(
            build_handoff(projection, max_chars=max_chars), legacy=legacy
        )
    warning = (
        "## Recovery status\n"
        "- Degraded: project recovery deferred due to writer contention.\n"
        f"- MCP recovery ID: `recovery:project:{slug}`\n"
    )
    handoff = build_handoff(
        projection,
        max_chars=max_chars - len(warning) - 2,
    )
    return ProjectHandoffResult(
        handoff.rstrip() + "\n\n" + warning,
        degraded=True,
        legacy=legacy,
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("project lease times must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _same_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return _same_identity(left, right) and (
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _require_slug(slug: str) -> str:
    if not isinstance(slug, str) or not slug or len(slug) > 256:
        raise ValueError("project slug must be a non-empty string up to 256 characters")
    if unicodedata.normalize("NFC", slug) != slug:
        raise ValueError("project slug must use NFC Unicode normalization")
    if slug != slug.lower():
        raise ValueError("project slug must be lowercase to prevent case aliases")
    if slug in {".", ".."} or "/" in slug or "\\" in slug:
        raise ValueError("project slug must be one safe path component")
    if slug.endswith((" ", ".")):
        raise ValueError("project slug cannot end in a dot or space")
    if any(
        character in '<>:"|?*'
        or character.isspace()
        or unicodedata.category(character).startswith("C")
        for character in slug
    ):
        raise ValueError("project slug contains a non-portable character")
    reserved = {"con", "prn", "aux", "nul"} | {
        f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
    }
    if slug.rstrip(" .").split(".", 1)[0].casefold() in reserved:
        raise ValueError("project slug uses a reserved Windows name")
    return slug


def _bootstrap_event_identity(slug: str, content: str) -> tuple[str, str, str]:
    normalized = unicodedata.normalize("NFC", slug)
    digest = hashlib.sha256(f"{normalized}\0{content}".encode()).hexdigest()
    return (
        f"bootstrap-state:{digest}",
        f"bootstrap-state:v2:{digest}",
        digest,
    )


def _bootstrap_operation_id(stable_hash: str, kind: str, index: int, value: str) -> str:
    digest = hashlib.sha256(
        f"{stable_hash}\0{kind}\0{index}\0{value}".encode()
    ).hexdigest()[:24]
    return f"bootstrap-{kind}-{digest}"


def legacy_state_project_root(content: str) -> str | None:
    match = re.search(
        r"^(?:-\s*)?(?:Source:\s*)?Project root:\s*`?([^`\r\n]+?)`?\s*$",
        content,
        re.MULTILINE | re.IGNORECASE,
    )
    return match.group(1).strip() if match else None


def _require_owner(owner: str) -> str:
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("project lease owner must be a non-empty string")
    return owner


def _lease_from_row(row: Mapping[str, object]) -> ProjectLease:
    return ProjectLease(
        slug=str(row["project"]),
        owner=str(row["owner"]),
        token=str(row["lease_token"]),
        epoch=int(row["fencing_epoch"]),
        expires_at=_parse_timestamp(str(row["expires_at"])),
        heartbeat_at=_parse_timestamp(str(row["heartbeat_at"])),
    )


class ProjectStore:
    """Persist project checkpoints through the Markdown transaction boundary."""

    def __init__(
        self,
        vault: Path,
        state_root: Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self.coordinator = MarkdownCoordinator(vault, state_root)
        self.vault = self.coordinator.vault
        self.state_root = self.coordinator.state_root
        self._clock = clock

    def _project_directory(self, slug: str) -> Path:
        slug = _require_slug(slug)
        projects = self.vault / "knowledge" / "projects"
        target = projects / slug
        try:
            target.relative_to(projects)
        except ValueError as exc:
            raise ValueError("project slug escapes the projects directory") from exc
        if projects.is_dir():
            key = unicodedata.normalize("NFC", slug).casefold()
            for entry in projects.iterdir():
                if (
                    unicodedata.normalize("NFC", entry.name).casefold() == key
                    and entry.name != slug
                ):
                    raise ValueError("project slug collides with an existing case alias")
        return target

    def acquire_lease(
        self,
        slug: str,
        owner: str,
        ttl: int = 30,
        *,
        token: str | None = None,
        now: datetime | None = None,
    ) -> ProjectLease:
        slug = _require_slug(slug)
        self._project_directory(slug)
        owner = _require_owner(owner)
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= _HEARTBEAT_SECONDS:
            raise ValueError("project lease ttl must be an integer greater than 10 seconds")
        if token is not None and (not isinstance(token, str) or not token):
            raise ValueError("project lease token must be a non-empty string")
        current_time = now or self._clock()
        expires_at = current_time + timedelta(seconds=ttl)
        with self.coordinator._connect() as database, begin_immediate(database):
            row = database.execute(
                "SELECT * FROM project_leases WHERE project = ?", (slug,)
            ).fetchone()
            if row is not None and _parse_timestamp(row["expires_at"]) > current_time:
                if row["owner"] != owner or token is None or row["lease_token"] != token:
                    raise ProjectLeaseBusy(f"project {slug!r} is leased by another invocation")
                database.execute(
                    "UPDATE project_leases SET expires_at = ?, heartbeat_at = ? "
                    "WHERE project = ? AND lease_token = ? AND fencing_epoch = ?",
                    (
                        _timestamp(expires_at),
                        _timestamp(current_time),
                        slug,
                        token,
                        row["fencing_epoch"],
                    ),
                )
                renewed = dict(row)
                renewed["expires_at"] = _timestamp(expires_at)
                renewed["heartbeat_at"] = _timestamp(current_time)
                return _lease_from_row(renewed)
            if token is not None:
                raise ProjectFenceError("project lease token is stale or expired")
            epoch = 1 if row is None else int(row["fencing_epoch"]) + 1
            token = secrets.token_hex(32)
            database.execute(
                "INSERT INTO project_leases "
                "(project, lease_token, fencing_epoch, owner, expires_at, heartbeat_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(project) DO UPDATE SET "
                "lease_token = excluded.lease_token, "
                "fencing_epoch = excluded.fencing_epoch, owner = excluded.owner, "
                "expires_at = excluded.expires_at, heartbeat_at = excluded.heartbeat_at",
                (
                    slug,
                    token,
                    epoch,
                    owner,
                    _timestamp(expires_at),
                    _timestamp(current_time),
                ),
            )
        return ProjectLease(slug, owner, token, epoch, expires_at, current_time)

    def heartbeat(
        self,
        lease: ProjectLease,
        ttl: int = 30,
        *,
        now: datetime | None = None,
    ) -> ProjectLease:
        if not isinstance(lease, ProjectLease):
            raise TypeError("lease must be a ProjectLease")
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= _HEARTBEAT_SECONDS:
            raise ValueError("project lease ttl must be an integer greater than 10 seconds")
        current_time = now or self._clock()
        expires_at = current_time + timedelta(seconds=ttl)
        with self.coordinator._connect() as database, begin_immediate(database):
            updated = database.execute(
                "UPDATE project_leases SET expires_at = ?, heartbeat_at = ? "
                "WHERE project = ? AND lease_token = ? AND fencing_epoch = ? "
                "AND owner = ? AND expires_at > ?",
                (
                    _timestamp(expires_at),
                    _timestamp(current_time),
                    lease.slug,
                    lease.token,
                    lease.epoch,
                    lease.owner,
                    _timestamp(current_time),
                ),
            ).rowcount
            if updated != 1:
                raise ProjectFenceError("project lease is stale or expired")
        return ProjectLease(
            lease.slug,
            lease.owner,
            lease.token,
            lease.epoch,
            expires_at,
            current_time,
        )

    def checkpoint(
        self,
        slug: str,
        event: Mapping[str, object],
        owner: str,
        *,
        writer_wait_seconds: float | None = None,
    ) -> CheckpointReceipt:
        slug = _require_slug(slug)
        _require_owner(owner)
        normalized_event = self.coordinator.normalize_project_checkpoint(slug, event)
        self.recover(slug, writer_wait_seconds=writer_wait_seconds)
        if normalized_event.get("trigger") != "legacy_state_bootstrap":
            bootstrap = self._legacy_bootstrap_event(slug, normalized_event)
            if bootstrap is not None:
                self.checkpoint(
                    slug,
                    bootstrap,
                    owner,
                    writer_wait_seconds=writer_wait_seconds,
                )
        lease = self.acquire_lease(slug, owner)
        try:
            reserved, duplicate = self._reserve(slug, normalized_event, lease)
            if duplicate and reserved.state == "committed":
                return self._receipt(reserved, duplicate=True)
            try:
                if writer_wait_seconds is None:
                    return self._project_reserved(reserved, lease)
                return self._project_reserved(
                    reserved, lease, writer_wait_seconds=writer_wait_seconds
                )
            except ProjectJournalRebuildRequired:
                self._set_checkpoint_state(slug, reserved.sequence, "quarantined")
                raise
            except TransactionFailure as exc:
                if exc.code == "precondition_failed":
                    self._set_checkpoint_state(slug, reserved.sequence, "quarantined")
                    raise ProjectFenceError(
                        "project lease changed before checkpoint apply"
                    ) from exc
                raise
        finally:
            self._release(lease)

    def _legacy_bootstrap_event(
        self, slug: str, event: Mapping[str, object]
    ) -> dict[str, object] | None:
        if self._read_journal_bytes(slug):
            return None
        state = self._read_projection_bytes(slug)
        if state is None:
            return None
        text = state.decode("utf-8", errors="replace")
        occurrence_id, idempotency_key, stable_hash = _bootstrap_event_identity(
            slug, text
        )
        provenance = event.get("provenance")
        if not isinstance(provenance, Mapping):
            return None
        worktree = provenance.get("worktree")
        owned_root = legacy_state_project_root(text)
        if owned_root is None or not isinstance(worktree, str):
            return None
        try:
            if Path(owned_root).resolve() != Path(worktree).resolve():
                return None
        except (OSError, ValueError):
            if owned_root != worktree:
                return None

        def values(body: str) -> list[str]:
            values = []
            for line in body.splitlines():
                value = re.sub(r"^-\s*(?:`[^`]+`:\s*)?", "", line).strip()
                if (
                    value
                    and value.lower() != "none"
                    and not (value.startswith("<") and value.endswith(">"))
                ):
                    values.append(value[:4096])
            return values

        sections: dict[str, list[str]] = {}
        matches = list(re.finditer(r"^##\s+(.+?)\s*$", text, re.MULTILINE))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            sections[match.group(1).strip().casefold()] = values(text[match.end() : end])

        def combined(*titles: str) -> list[str]:
            return [
                value
                for title in titles
                for value in sections.get(title.casefold(), [])
            ]

        goal = combined("Goal", "Project goal")
        phase = combined("Phase", "Current phase")
        task = combined("Current task", "Current work")
        next_actions = combined("Next actions", "Next steps")
        decisions = combined("Recent decisions", "Decisions")
        blockers = combined("Open blockers", "Blockers", "Open threads")
        changed_files = combined("Changed files", "Files changed")
        commands = combined("Commands", "Commands run")
        verification = combined("Verification", "Test results")

        mapped_titles = {
            "goal",
            "project goal",
            "phase",
            "current phase",
            "current task",
            "current work",
            "next actions",
            "next steps",
            "recent decisions",
            "decisions",
            "open blockers",
            "blockers",
            "open threads",
            "changed files",
            "files changed",
            "commands",
            "commands run",
            "verification",
            "test results",
        }
        legacy_parts: list[str] = []
        summary = re.search(r"^One-sentence summary:\s*(.+)$", text, re.MULTILINE)
        if summary and not summary.group(1).strip().startswith("<"):
            legacy_parts.append(f"Summary: {summary.group(1).strip()}")
        for title, section_values in sections.items():
            if title in mapped_titles:
                continue
            if title == "source":
                section_values = [
                    value
                    for value in section_values
                    if not re.match(r"Project root:", value, re.IGNORECASE)
                ]
            if section_values:
                legacy_parts.append(
                    f"{title.title()}:\n" + "\n".join(f"- {value}" for value in section_values)
                )
        legacy_context = "\n\n".join(legacy_parts)[:16384]
        if not any(
            (
                goal,
                phase,
                task,
                next_actions,
                decisions,
                blockers,
                changed_files,
                commands,
                verification,
                legacy_context,
            )
        ):
            return None
        close = {"id": "checkpoint-none", "action": "close", "value": ""}

        def scalar(name: str, items: list[str]) -> dict[str, str]:
            if not items:
                return dict(close)
            value = items[-1]
            return {
                "id": _bootstrap_operation_id(stable_hash, name, 1, value),
                "action": "upsert",
                "value": value,
            }

        def operations(name: str, items: list[str], limit: int) -> list[dict[str, str]]:
            return [
                {
                    "id": _bootstrap_operation_id(stable_hash, name, index, value),
                    "action": "upsert",
                    "value": value,
                }
                for index, value in enumerate(items[:limit], 1)
            ]

        delta: dict[str, object] = {
            "goal": scalar("goal", goal),
            "goal_operations": [],
            "phase": scalar("phase", phase),
            "phase_operations": [],
            "current_task": scalar("task", task),
            "current_task_operations": [],
            "next_actions": operations("next", next_actions, 10),
            "decisions": operations("decision", decisions, 100),
            "blockers": operations("blocker", blockers, 100),
            "changed_files": operations("file", changed_files, 100),
            "commands": operations("command", commands, 100),
            "verification": operations("verify", verification, 100),
            "legacy_context": legacy_context,
        }
        seed_provenance = dict(provenance)
        seed_provenance["source_event"] = f"bootstrap-state:{stable_hash}"
        return {
            "schema_version": "project-checkpoint/v1",
            "occurrence_id": occurrence_id,
            "idempotency_key": idempotency_key,
            "provenance": seed_provenance,
            "trigger": "legacy_state_bootstrap",
            "reason": "bootstrap_legacy_state",
            "delta": delta,
            "evidence_event_ids": [f"bootstrap-state:{stable_hash}"],
        }

    def legacy_projection(
        self, slug: str, project_root: Path | str
    ) -> ProjectProjection | None:
        """Parse an owned pre-journal state without mutating it."""
        event = self._legacy_bootstrap_event(
            slug,
            {
                "provenance": {
                    "agent": "legacy-state",
                    "session": "legacy-state",
                    "worktree": str(project_root),
                    "branch": "unknown",
                    "source_event": "legacy-state",
                }
            },
        )
        if event is None:
            return None
        delta = event["delta"]
        assert isinstance(delta, Mapping)
        projection = ProjectProjection(project=slug)
        for name in ("goal", "phase", "current_task"):
            operation = delta[name]
            assert isinstance(operation, Mapping)
            self._reduce(getattr(projection, name), operation)
            scalar_operations = delta.get(f"{name}_operations", [])
            assert isinstance(scalar_operations, list)
            for scalar_operation in scalar_operations:
                assert isinstance(scalar_operation, Mapping)
                self._reduce(getattr(projection, name), scalar_operation)
        for name in _MAX_LIST_ITEMS:
            operations = delta[name]
            assert isinstance(operations, list)
            for operation in operations:
                assert isinstance(operation, Mapping)
                self._reduce(getattr(projection, name), operation)
        context = delta.get("legacy_context")
        if isinstance(context, str):
            projection.legacy_context = context
        return projection

    def recover(
        self,
        slug: str | None = None,
        *,
        writer_wait_seconds: float | None = None,
    ) -> list[CheckpointReceipt]:
        if slug is not None:
            _require_slug(slug)
            self._project_directory(slug)
        with self.coordinator._connect() as database:
            query = "SELECT project, sequence FROM project_checkpoints WHERE state != 'committed'"
            parameters: tuple[object, ...] = ()
            if slug is not None:
                query += " AND project = ?"
                parameters = (slug,)
            candidates = {(row["project"], row["sequence"]) for row in database.execute(query, parameters)}

        transaction_records = (
            self.coordinator.recover()
            if writer_wait_seconds is None
            else self.coordinator.recover(
                writer_wait_seconds=writer_wait_seconds
            )
        )
        records = {record.id: record for record in transaction_records}
        recovered: list[CheckpointReceipt] = []
        replay: list[ProjectCheckpointReservation] = []
        with self.coordinator._connect() as database, begin_immediate(database):
            for project, sequence in sorted(candidates):
                committed = database.execute(
                    "SELECT * FROM project_checkpoints WHERE project = ? "
                    "AND sequence = ? AND state = 'committed'",
                    (project, sequence),
                ).fetchone()
                if committed is not None:
                    recovered.append(self._receipt(committed))
            query = "SELECT * FROM project_checkpoints WHERE state IN ('prepared', 'reserved')"
            parameters = ()
            if slug is not None:
                query += " AND project = ?"
                parameters = (slug,)
            rows = list(database.execute(query + " ORDER BY project, sequence", parameters))
            for row in rows:
                transaction_id = row["transaction_id"]
                record = records.get(transaction_id)
                if record is None and transaction_id:
                    record = self.coordinator._record(transaction_id)
                if record is not None and record.state == "committed":
                    database.execute(
                        "UPDATE project_checkpoints SET state = 'committed' "
                        "WHERE project = ? AND sequence = ?",
                        (row["project"], row["sequence"]),
                    )
                    database.execute(
                        "UPDATE project_checkpoint_attempts SET state = 'committed' "
                        "WHERE transaction_id = ?",
                        (transaction_id,),
                    )
                    if (row["project"], row["sequence"]) in candidates:
                        recovered.append(self._receipt(row))
                elif record is not None and record.state in {"conflicted", "quarantined"}:
                    database.execute(
                        "UPDATE project_checkpoints SET state = 'quarantined' "
                        "WHERE project = ? AND sequence = ?",
                        (row["project"], row["sequence"]),
                    )
                    database.execute(
                        "UPDATE project_checkpoint_attempts SET state = 'quarantined' "
                        "WHERE transaction_id = ?",
                        (transaction_id,),
                    )
                elif record is None:
                    replay.append(self.coordinator._project_reservation(row))
        for row in replay:
            project = row.project
            try:
                lease = self.acquire_lease(project, "project-recovery")
            except ProjectLeaseBusy:
                continue
            try:
                recovered.append(
                    self._project_reserved(
                        row, lease, writer_wait_seconds=writer_wait_seconds
                    )
                )
            except ProjectPendingPriorError:
                continue
            except TransactionFailure as exc:
                if exc.code == "precondition_failed":
                    self._set_checkpoint_state(
                        project, row.sequence, "quarantined"
                    )
                    continue
                raise
            finally:
                self._release(lease)
        return recovered

    def read_journal(self, slug: str) -> str:
        content = self._read_journal_bytes(slug)
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("project journal must be UTF-8") from exc

    def _read_journal_bytes(self, slug: str) -> bytes:
        path = self._project_directory(slug) / "journal.md"
        return self._read_bounded_regular_file(
            path,
            max_bytes=MAX_JOURNAL_BYTES,
            label="project journal",
        ) or b""

    def _read_projection_bytes(self, slug: str) -> bytes | None:
        path = self._project_directory(slug) / "state.md"
        return self._read_bounded_regular_file(
            path,
            max_bytes=MAX_PROJECTION_BYTES,
            label="project projection",
        )

    def _read_bounded_regular_file(
        self,
        path: Path,
        *,
        max_bytes: int,
        label: str,
    ) -> bytes | None:
        self._validate_file_parent(path.parent, label)
        try:
            before = os.lstat(path)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(before.st_mode) or _is_reparse(before):
            raise ProjectJournalReadError("unsafe_path", f"{label} is a link")
        if not stat.S_ISREG(before.st_mode):
            raise ProjectJournalReadError(
                "not_regular", f"{label} is not a regular file"
            )
        if before.st_size > max_bytes:
            raise ProjectJournalReadError(
                "too_large", f"{label} exceeds {max_bytes} bytes"
            )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(path, flags)
        except (OSError, ValueError) as exc:
            raise ProjectJournalReadError(
                "unsafe_path", f"{label} cannot be opened safely"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            if not _same_identity(before, opened):
                raise ProjectJournalReadError("changed", f"{label} changed before open")
            if not stat.S_ISREG(opened.st_mode):
                raise ProjectJournalReadError(
                    "not_regular", f"{label} is not a regular file"
                )
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 1024 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            after = os.fstat(descriptor)
            if not _same_snapshot(opened, after) or len(content) != after.st_size:
                raise ProjectJournalReadError(
                    "changed", f"{label} changed while reading"
                )
            if len(content) > max_bytes:
                raise ProjectJournalReadError(
                    "too_large", f"{label} exceeds {max_bytes} bytes"
                )
            return content
        finally:
            os.close(descriptor)

    def _validate_file_parent(self, parent: Path, label: str) -> None:
        try:
            relative = parent.relative_to(self.vault)
        except ValueError as exc:
            raise ProjectJournalReadError(
                "unsafe_path", f"{label} parent escapes the vault"
            ) from exc
        current = self.vault
        for part in relative.parts:
            current = current / part
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                return
            if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
                raise ProjectJournalReadError(
                    "unsafe_path", f"{label} parent traverses a link"
                )
            if not stat.S_ISDIR(metadata.st_mode):
                raise ProjectJournalReadError(
                    "unsafe_path", f"{label} parent is not a directory"
                )

    def _ensure_project_directory(self, slug: str) -> None:
        target = self._project_directory(slug)
        relative = target.relative_to(self.vault)
        current = self.vault
        for part in relative.parts:
            current = current / part
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                try:
                    os.mkdir(current)
                except FileExistsError:
                    pass
                metadata = os.lstat(current)
            if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
                raise ProjectJournalReadError(
                    "unsafe_path", "project directory traverses a link"
                )
            if not stat.S_ISDIR(metadata.st_mode):
                raise ProjectJournalReadError(
                    "unsafe_path", "project path is not a directory"
                )

    def render_state(
        self,
        events: Sequence[Mapping[str, object]],
        *,
        _validated: bool = False,
    ) -> bytes:
        ordered = sorted(events, key=lambda item: int(item["sequence"]))
        active: dict[str, dict[str, str]] = {
            name: {}
            for name in (
                "goal",
                "phase",
                "current_task",
                "next_actions",
                "decisions",
                "blockers",
                "changed_files",
                "commands",
                "verification",
            )
        }
        project = "project"
        project_root = ""
        legacy_context = ""
        last_sequence = 0
        for event in ordered:
            if not _validated:
                validate_schema(event, _SCHEMA)
            project = str(event["project"])
            provenance = event["provenance"]
            assert isinstance(provenance, Mapping)
            project_root = str(provenance["worktree"])
            sequence = int(event["sequence"])
            if sequence <= last_sequence:
                raise ValueError("project journal sequences must increase strictly")
            last_sequence = sequence
            delta = event["delta"]
            assert isinstance(delta, Mapping)
            context = delta.get("legacy_context")
            if isinstance(context, str) and context:
                legacy_context = context
            for name in ("goal", "phase", "current_task"):
                operation = delta[name]
                assert isinstance(operation, Mapping)
                self._reduce(active[name], operation)
            for name in ("goal", "phase"):
                scalar_operations = delta.get(f"{name}_operations", [])
                assert isinstance(scalar_operations, list)
                for operation in scalar_operations:
                    assert isinstance(operation, Mapping)
                    self._reduce(active[name], operation)
            task_operations = delta.get("current_task_operations", [])
            assert isinstance(task_operations, list)
            for operation in task_operations:
                assert isinstance(operation, Mapping)
                self._reduce(active["current_task"], operation)
            for name in _MAX_LIST_ITEMS:
                operations = delta[name]
                assert isinstance(operations, list)
                for operation in operations:
                    assert isinstance(operation, Mapping)
                    self._reduce(active[name], operation)

        lines = [
            "---",
            "type: project-state",
            f'title: "{project} - State"',
            f"project: {project}",
            "generated: true",
            f"last_applied_sequence: {last_sequence}",
            "---",
            f"# {project} - State",
            "",
            "> Generated from `journal.md`. Do not edit this file directly.",
            "",
            "## Source",
            f"- Project root: `{project_root}`",
            "",
        ]
        for title, name in (
            ("Goal", "goal"),
            ("Phase", "phase"),
            ("Current task", "current_task"),
            ("Next actions", "next_actions"),
            ("Recent decisions", "decisions"),
            ("Open blockers", "blockers"),
            ("Changed files", "changed_files"),
            ("Commands", "commands"),
            ("Verification", "verification"),
        ):
            lines.extend((f"## {title}",))
            values = list(active[name].items())
            limit = 1 if name in {"goal", "phase", "current_task"} else _MAX_LIST_ITEMS[name]
            values = values[-limit:]
            if values:
                lines.extend(f"- `{item_id}`: {value}" for item_id, value in values)
            else:
                lines.append("- None")
            lines.append("")
        lines.append("## Legacy context")
        lines.append(legacy_context or "- None")
        lines.append("")
        return ("\n".join(lines).rstrip() + "\n").encode("utf-8")

    def projection(self, slug: str) -> ProjectProjection:
        """Reduce the current bounded journal into an in-memory projection."""
        records = self._journal_events(slug, self._read_journal_bytes(slug))
        active = ProjectProjection(project=slug)
        for event in records:
            active.project = str(event["project"])
            active.last_applied_sequence = int(event["sequence"])
            delta = event["delta"]
            assert isinstance(delta, Mapping)
            context = delta.get("legacy_context")
            if isinstance(context, str) and context:
                active.legacy_context = context
            for name in ("goal", "phase", "current_task"):
                operation = delta[name]
                assert isinstance(operation, Mapping)
                self._reduce(getattr(active, name), operation)
            for name in ("goal", "phase"):
                scalar_operations = delta.get(f"{name}_operations", [])
                assert isinstance(scalar_operations, list)
                for operation in scalar_operations:
                    assert isinstance(operation, Mapping)
                    self._reduce(getattr(active, name), operation)
            task_operations = delta.get("current_task_operations", [])
            assert isinstance(task_operations, list)
            for operation in task_operations:
                assert isinstance(operation, Mapping)
                self._reduce(active.current_task, operation)
            for name in _MAX_LIST_ITEMS:
                operations = delta[name]
                assert isinstance(operations, list)
                for operation in operations:
                    assert isinstance(operation, Mapping)
                    self._reduce(getattr(active, name), operation)
        return active

    @staticmethod
    def _reduce(target: dict[str, str], operation: Mapping[str, object]) -> None:
        item_id = str(operation["id"])
        if operation["action"] == "close":
            target.pop(item_id, None)
            return
        value = " ".join(str(operation["value"]).split())[:_MAX_VALUE_CHARS]
        target.pop(item_id, None)
        target[item_id] = value

    def _reserve(
        self,
        slug: str,
        event: Mapping[str, object],
        lease: ProjectLease,
    ) -> tuple[ProjectCheckpointReservation, bool]:
        if not isinstance(event, Mapping):
            raise TypeError("checkpoint event must be a mapping")
        precondition = {
            "project": slug,
            "lease_token": lease.token,
            "fencing_epoch": lease.epoch,
            "expires_at": _timestamp(lease.expires_at),
        }
        reservation = self.coordinator.reserve_project_checkpoint(
            slug, event, precondition
        )
        validate_schema(json.loads(reservation.event_json), _SCHEMA)
        return reservation, reservation.duplicate

    def _project_reserved(
        self,
        row: ProjectCheckpointReservation,
        lease: ProjectLease,
        *,
        writer_wait_seconds: float | None = None,
    ) -> CheckpointReceipt:
        slug = row.project
        sequence = row.sequence
        event = json.loads(row.event_json)
        with self.coordinator._connect() as database:
            self.coordinator._check_project_head(database, slug, sequence)
        lease = self.heartbeat(lease)
        current_journal = self._read_journal_bytes(slug)
        records = self._journal_events(slug, current_journal)
        lease = self.heartbeat(lease)
        matching = [item for item in records if item["sequence"] == sequence]
        journal_head = int(records[-1]["sequence"]) if records else 0
        if not matching and journal_head != sequence - 1:
            raise ProjectJournalRebuildRequired(slug, sequence, journal_head)
        if not matching:
            journal = current_journal or JOURNAL_HEADER.encode("utf-8")
            if not journal.endswith(b"\n"):
                raise ValueError("project journal must end with a newline")
            journal += canonical_json_bytes(event) + b"\n"
            records.append(event)
        else:
            if canonical_json_bytes(matching[0]) != canonical_json_bytes(event):
                raise ValueError("project journal sequence is bound to another event")
            journal = current_journal
        if len(records) > MAX_JOURNAL_EVENTS:
            raise ProjectJournalReadError(
                "too_many_events",
                f"project journal exceeds {MAX_JOURNAL_EVENTS} event lines",
            )
        if len(journal) > MAX_JOURNAL_BYTES:
            raise ProjectJournalReadError(
                "too_large", f"project journal exceeds {MAX_JOURNAL_BYTES} bytes"
            )
        lease = self.heartbeat(lease)
        state = self.render_state(records, _validated=True)
        lease = self.heartbeat(lease)
        journal_path = f"knowledge/projects/{slug}/journal.md"
        state_path = f"knowledge/projects/{slug}/state.md"
        current_state = self._read_projection_bytes(slug)
        changes = [
            MarkdownChange.replace(
                journal_path, journal, max_before_bytes=MAX_JOURNAL_BYTES
            )
            if current_journal
            else MarkdownChange.create(
                journal_path, journal, max_before_bytes=MAX_JOURNAL_BYTES
            ),
            MarkdownChange.replace(
                state_path, state, max_before_bytes=MAX_PROJECTION_BYTES
            )
            if current_state is not None
            else MarkdownChange.create(
                state_path, state, max_before_bytes=MAX_PROJECTION_BYTES
            ),
        ]
        preconditions: dict[str, object] = {
            "project_lease": {
                "project": slug,
                "lease_token": lease.token,
                "fencing_epoch": lease.epoch,
                "expires_at": _timestamp(lease.expires_at),
            },
            journal_path: sha256_bytes(current_journal) if current_journal else ABSENT,
            state_path: sha256_bytes(current_state)
            if current_state is not None
            else ABSENT,
        }
        lease = self.heartbeat(lease)
        self._ensure_project_directory(slug)
        preconditions["project_lease"] = {
            "project": slug,
            "lease_token": lease.token,
            "fencing_epoch": lease.epoch,
            "expires_at": _timestamp(lease.expires_at),
        }
        transaction = self.coordinator.prepare(
            changes,
            operation_id=row.operation_id,
            preconditions=preconditions,
            project_reservation=row,
        )
        lease = self.heartbeat(lease)
        self.coordinator.refresh_project_lease_precondition(
            transaction.id,
            {
                "project": slug,
                "lease_token": lease.token,
                "fencing_epoch": lease.epoch,
                "expires_at": _timestamp(lease.expires_at),
            },
        )
        lease = self.heartbeat(lease)
        self.coordinator.refresh_project_lease_precondition(
            transaction.id,
            {
                "project": slug,
                "lease_token": lease.token,
                "fencing_epoch": lease.epoch,
                "expires_at": _timestamp(lease.expires_at),
            },
        )
        committed = (
            self.coordinator.apply(transaction.id)
            if writer_wait_seconds is None
            else self.coordinator.apply(
                transaction.id, writer_wait_seconds=writer_wait_seconds
            )
        )
        if committed.state != "committed":
            raise RuntimeError("project checkpoint transaction did not commit")
        refreshed = ProjectCheckpointReservation(
            project=row.project,
            sequence=row.sequence,
            occurrence_id=row.occurrence_id,
            idempotency_key=row.idempotency_key,
            event_json=row.event_json,
            operation_id=row.operation_id,
            attempt_number=row.attempt_number,
            state="committed",
            transaction_id=transaction.id,
            parent_operation_id=row.parent_operation_id,
            duplicate=row.duplicate,
        )
        return self._receipt(refreshed)

    def _journal_events(self, slug: str, content: bytes) -> list[dict[str, object]]:
        if not content:
            return []
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("project journal must be UTF-8") from exc
        if not text.startswith(JOURNAL_HEADER):
            raise ValueError("project journal header is invalid")
        lines = [line for line in text.removeprefix(JOURNAL_HEADER).splitlines() if line]
        if len(lines) > MAX_JOURNAL_EVENTS:
            raise ProjectJournalReadError(
                "too_many_events",
                f"project journal exceeds {MAX_JOURNAL_EVENTS} event lines",
            )
        events: list[dict[str, object]] = []
        previous = 0
        for line in lines:
            event = json.loads(line)
            if canonical_json_bytes(event).decode("utf-8") != line:
                raise ValueError("project journal event is not canonical JSON")
            validate_schema(event, _SCHEMA)
            if event["project"] != slug or event["sequence"] <= previous:
                raise ValueError("project journal sequence or slug is invalid")
            previous = int(event["sequence"])
            events.append(event)
        return events

    def _release(self, lease: ProjectLease) -> None:
        now = self._clock()
        with self.coordinator._connect() as database, begin_immediate(database):
            database.execute(
                "UPDATE project_leases SET expires_at = ? WHERE project = ? "
                "AND lease_token = ? AND fencing_epoch = ?",
                (_timestamp(now), lease.slug, lease.token, lease.epoch),
            )

    def _set_checkpoint_state(self, slug: str, sequence: int, state: str) -> None:
        with self.coordinator._connect() as database, begin_immediate(database):
            database.execute(
                "UPDATE project_checkpoints SET state = ? WHERE project = ? AND sequence = ?",
                (state, slug, sequence),
            )
            database.execute(
                "UPDATE project_checkpoint_attempts SET state = ? WHERE operation_id = "
                "(SELECT operation_id FROM project_checkpoints "
                "WHERE project = ? AND sequence = ?)",
                (state, slug, sequence),
            )

    @staticmethod
    def _receipt(
        row: Mapping[str, object] | ProjectCheckpointReservation,
        duplicate: bool = False,
    ) -> CheckpointReceipt:
        if isinstance(row, ProjectCheckpointReservation):
            return CheckpointReceipt(
                project=row.project,
                sequence=row.sequence,
                occurrence_id=row.occurrence_id,
                idempotency_key=row.idempotency_key,
                transaction_id=row.transaction_id,
                duplicate=duplicate,
            )
        return CheckpointReceipt(
            project=str(row["project"]),
            sequence=int(row["sequence"]),
            occurrence_id=str(row["occurrence_id"]),
            idempotency_key=str(row["idempotency_key"]),
            transaction_id=str(row["transaction_id"]) if row["transaction_id"] else None,
            duplicate=duplicate,
        )
