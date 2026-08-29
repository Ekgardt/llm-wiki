"""Fenced append-only project journals and deterministic state projections."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from context_budget import ContextItem
    from operational_ownership import OwnerLease

from markdown_transaction import (
    ABSENT,
    MarkdownChange,
    MarkdownCoordinator,
    ProjectCheckpointReservation,
    ProjectPendingPriorError,
    TransactionFailure,
    active_or_legacy_coordinator,
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

    def __reduce__(self):
        return (self.__class__, (self.project, self.sequence, self.journal_head))


class ProjectJournalReadError(RuntimeError):
    """A project file cannot be safely read as a bounded regular file."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.status = code

    def __reduce__(self):
        return (self.__class__, (self.code, str(self)))


@dataclass(frozen=True)
class ProjectLease:
    slug: str
    owner: str
    token: str
    epoch: int
    expires_at: datetime
    heartbeat_at: datetime
    _ownership: OwnerLease | None = field(default=None, repr=False, compare=False)
    _release_canonical: bool = field(default=False, repr=False, compare=False)

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


def _yaml_scalar(value: str) -> str:
    """One double-quoted YAML scalar.

    A project slug is not guaranteed to be plain text: redaction can leave
    brackets in it, and an unquoted `[...]` parses as a list, which the corpus
    reader refuses. Quoting keeps every slug a string.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


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
    items: tuple[ContextItem, ...] = ()


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

    _STATIC_REASONS = {
        "session_start": "session_start_recovery",
        "pre_compact": "before_compaction",
        "compaction_confirmed": "after_compaction",
        "session_end": "session_end",
    }
    _DIRTY_REASONS = {"stop": "dirty_stop", "session_idle": "dirty_idle"}
    _PROGRESS_TYPES = frozenset({"compaction_confirmed", "token_usage"})
    _CHANGE_TYPES = frozenset({"file_changed", "mutation"})

    def observe(
        self,
        event: Mapping[str, object],
        *,
        now: datetime | None = None,
        commit: bool = True,
    ) -> CheckpointDecision | None:
        current_time = _observation_time(event, now)
        if self._already_observed(event):
            return None
        decision = self._decide(event, current_time)
        if decision is not None and commit:
            self.commit_observation(decision, outcome=_observation_outcome(decision))
        return decision

    def _already_observed(self, event: Mapping[str, object]) -> bool:
        """Record this event's identity, reporting whether it was already seen."""
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            return False
        if event_id in self.observed_event_ids:
            return True
        self.observed_event_ids[event_id] = None
        while len(self.observed_event_ids) > 256:
            self.observed_event_ids.pop(next(iter(self.observed_event_ids)))
        return False

    def _decide(
        self, event: Mapping[str, object], current_time: datetime
    ) -> CheckpointDecision | None:
        event_type = str(event.get("type") or "")
        dirty_active = self._track_dirty(event, current_time)
        significant = self._significant(event, event_type)
        if significant:
            self.significant_count += 1
        reason, forced, token_threshold, dirty_threshold = self._resolve_reason(
            event,
            event_type,
            dirty_active=dirty_active,
            significant=significant,
            current_time=current_time,
        )
        if reason is None or self._throttled(event_type, forced, current_time):
            return None
        return CheckpointDecision(
            reason,
            forced,
            maintenance=reason == "session_start_recovery",
            checkpoint_at=current_time,
            dirty_threshold=dirty_threshold,
            next_token_threshold=_next_token_threshold(token_threshold),
        )

    def _track_dirty(
        self, event: Mapping[str, object], current_time: datetime
    ) -> bool:
        dirty = event.get("dirty")
        if dirty is True and self.dirty_since is None:
            self.dirty_since = current_time
        elif dirty is False:
            self.dirty_since = None
            self.dirty_thresholds.clear()
        return _dirty_active(dirty, self.dirty_since)

    def _significant(self, event: Mapping[str, object], event_type: str) -> bool:
        if event_type in self._CHANGE_TYPES:
            return event.get("changed") is True or event.get("significant") is True
        return event_type in self._SIGNIFICANT_TYPES

    def _resolve_reason(
        self,
        event: Mapping[str, object],
        event_type: str,
        *,
        dirty_active: bool,
        significant: bool,
        current_time: datetime,
    ) -> tuple[str | None, bool, int | None, int | None]:
        """(reason, forced, reached token threshold, reached dirty threshold)."""
        reason, forced, token_threshold = self._event_reason(
            event, event_type, dirty_active=dirty_active
        )
        if reason is not None:
            return (reason, forced, token_threshold, None)
        reason, dirty_threshold = self._dirty_elapsed_reason(current_time)
        if reason is not None:
            return (reason, False, None, dirty_threshold)
        return (self._cadence_reason(significant), False, None, None)

    def _event_reason(
        self, event: Mapping[str, object], event_type: str, *, dirty_active: bool
    ) -> tuple[str | None, bool, int | None]:
        if event_type in self._PROGRESS_TYPES:
            self.host_progress_signals = True
        if event_type == "token_usage":
            return self._token_reason(event)
        return (
            self._typed_reason(event, event_type, dirty_active=dirty_active),
            False,
            None,
        )

    def _typed_reason(
        self, event: Mapping[str, object], event_type: str, *, dirty_active: bool
    ) -> str | None:
        static = self._STATIC_REASONS.get(event_type)
        if static is not None:
            return static
        if dirty_active and event_type in self._DIRTY_REASONS:
            return self._DIRTY_REASONS[event_type]
        return self._change_reason(event, event_type)

    def _change_reason(
        self, event: Mapping[str, object], event_type: str
    ) -> str | None:
        if event_type in self._CHANGE_TYPES and event.get("significant") is True:
            return "file_change"
        return self._REASONS.get(event_type)

    def _token_reason(
        self, event: Mapping[str, object]
    ) -> tuple[str | None, bool, int | None]:
        percent = event.get("percent")
        if not isinstance(percent, (int, float)) or isinstance(percent, bool):
            return (None, False, None)
        return self._token_threshold_reason(min(int(percent), 80))

    def _token_threshold_reason(
        self, bounded: int
    ) -> tuple[str | None, bool, int | None]:
        if bounded < self.token_threshold:
            return (None, False, None)
        if bounded >= 80:
            return ("token_forced_80", True, 80)
        return (f"token_{self.token_threshold}", False, self.token_threshold)

    def _dirty_elapsed_reason(
        self, current_time: datetime
    ) -> tuple[str | None, int | None]:
        if self.dirty_since is None:
            return (None, None)
        elapsed = current_time - self.dirty_since
        for minutes in (10, 30):
            if elapsed >= timedelta(minutes=minutes) and minutes not in self.dirty_thresholds:
                return (f"dirty_{minutes}_minutes", minutes)
        return (None, None)

    def _cadence_reason(self, significant: bool) -> str | None:
        if significant and not self.host_progress_signals and self.significant_count % 20 == 0:
            return f"significant_event_{self.significant_count}"
        return None

    def _throttled(
        self, event_type: str, forced: bool, current_time: datetime
    ) -> bool:
        """Whether a non-bypassing reason falls inside the 30 second cadence floor."""
        if event_type in self._BYPASS_TYPES or forced:
            return False
        if self.last_checkpoint_at is None:
            return False
        return current_time - self.last_checkpoint_at < timedelta(seconds=30)

    def commit_observation(
        self,
        decision: CheckpointDecision | None,
        *,
        outcome: str,
    ) -> None:
        """Finalize reducer state after the observation's required action succeeds."""
        expected = _expected_outcome(decision)
        if outcome != expected:
            raise ValueError(f"expected {expected} observation outcome")
        if decision is None or decision.maintenance:
            return
        self._apply_checkpoint_state(decision)

    def _apply_checkpoint_state(self, decision: CheckpointDecision) -> None:
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
        return cls(
            host_progress_signals=value.get("progress_signal_observed") is True,
            significant_count=int(value.get("significant_count") or 0),
            token_threshold=int(value.get("token_threshold") or 60),
            dirty_since=_optional_timestamp(value.get("dirty_since")),
            dirty_thresholds=_int_list(value.get("dirty_thresholds")),
            last_checkpoint_at=_optional_timestamp(value.get("last_checkpoint_at")),
            observed_event_ids=_str_list(value.get("observed_event_ids")),
        )


def _observation_time(event: Mapping[str, object], now: datetime | None) -> datetime:
    """The timezone-aware UTC instant one checkpoint observation is measured at."""
    if not isinstance(event, Mapping):
        raise TypeError("checkpoint observation must be a mapping")
    current_time = now or _utc_now()
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("checkpoint observation time must be timezone-aware")
    return current_time.astimezone(timezone.utc)


def _observation_outcome(decision: CheckpointDecision) -> str:
    return "maintenance" if decision.maintenance else "checkpoint"


def _expected_outcome(decision: CheckpointDecision | None) -> str:
    if decision is None:
        return "no_checkpoint"
    return _observation_outcome(decision)


def _next_token_threshold(threshold: int | None) -> int | None:
    return None if threshold is None else threshold + 10


def _dirty_active(dirty: object, dirty_since: datetime | None) -> bool:
    return dirty is True or dirty is None and dirty_since is not None


def _optional_timestamp(value: object) -> datetime | None:
    return _parse_timestamp(value) if isinstance(value, str) else None


def _int_list(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    return [int(item) for item in value]


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _require_handoff_bounds(project: ProjectProjection, max_actions: int) -> int:
    if not isinstance(project, ProjectProjection):
        raise TypeError("project must be a ProjectProjection")
    if max_actions < 0:
        raise ValueError("handoff bounds must be positive")
    return min(max_actions, 3)


def _handoff_section_text(title: str, values: Sequence[tuple[str, str]]) -> str:
    return "\n".join(
        [f"## {title}", *(f"- `{item_id}`: {value}" for item_id, value in values)]
    )


def _handoff_identifiers(project: ProjectProjection) -> str:
    return "\n".join(
        (
            "## MCP identifiers",
            f"- `project:{project.project}`",
            f"- `sequence:{project.last_applied_sequence}`",
        )
    )


def _handoff_sections(
    project: ProjectProjection, max_actions: int
) -> list[tuple[str, str, str, bool]]:
    sections: list[tuple[str, str, str, bool]] = [
        ("title", f"# Project handoff: {project.project}", "handoff", True)
    ]
    named = (
        ("Active goal", list(project.goal.items())[-1:], "handoff"),
        ("Active task", list(project.current_task.items())[-1:], "handoff"),
        ("Next actions", list(project.next_actions.items())[-max_actions:], "handoff"),
        ("Blockers", list(project.blockers.items()), "blocker"),
        ("Recent decisions", list(project.decisions.items())[-5:], "decision"),
    )
    for title, values, priority_class in named:
        if values:
            sections.append(
                (title, _handoff_section_text(title, values), priority_class, True)
            )
    if project.legacy_context:
        sections.append(
            ("legacy", f"## Legacy context\n{project.legacy_context}", "history", True)
        )
    sections.append(("identifiers", _handoff_identifiers(project), "handoff", True))
    return sections


def build_handoff_items(
    project: ProjectProjection,
    *,
    max_actions: int = 3,
) -> tuple[ContextItem, ...]:
    """Build the complete semantic items used for SessionStart handoff."""
    max_actions = _require_handoff_bounds(project, max_actions)

    from context_budget import ContextItem

    return tuple(
        ContextItem(
            item_id=f"handoff:{index:02d}:{name}",
            text=text,
            source=f"project:{project.project}",
            priority=index + 1,
            relevance=1.0 if mandatory else 0.8,
            confidence="high",
            freshness="fresh",
            token_cost=len(text.encode("utf-8")),
            mandatory=mandatory,
            representation="l1",
            parent_id=f"project:{project.project}",
            priority_class=priority_class,
        )
        for index, (name, text, priority_class, mandatory) in enumerate(
            _handoff_sections(project, max_actions)
        )
    )


def _render_handoff_items(
    items: Sequence[ContextItem],
    *,
    max_chars: int,
) -> str:
    from context_budget import DEFAULT_CONTEXT_BUDGET, BudgetExceededError
    from context_compiler import compile_context_items

    try:
        return compile_context_items(
            items,
            budget=DEFAULT_CONTEXT_BUDGET,
            emergency_byte_cap=max_chars,
            per_source_cap=len(items),
            per_parent_cap=len(items),
        ).text + "\n"
    except BudgetExceededError as error:
        return error.failure.render(max_bytes=max_chars)


def build_handoff(
    project: ProjectProjection,
    *,
    max_actions: int = 3,
    max_chars: int = 2400,
) -> str:
    """Render the bounded operational subset used for SessionStart handoff."""
    if max_chars < 1:
        raise ValueError("handoff bounds must be positive")
    return _render_handoff_items(
        build_handoff_items(project, max_actions=max_actions),
        max_chars=max_chars,
    )


def _legacy_or_current_projection(
    store: ProjectStore, slug: str, project_root: Path | str | None
) -> tuple[ProjectProjection, bool]:
    """The journal projection, or an owned pre-journal one when the journal is empty."""
    projection = store.projection(slug)
    if project_root is None or projection.last_applied_sequence != 0:
        return projection, False
    candidate = store.legacy_projection(slug, project_root)
    if candidate is None:
        return projection, False
    return candidate, True


def _recovery_warning_text(slug: str) -> str:
    return (
        "## Recovery status\n"
        "- Degraded: project recovery deferred due to writer contention.\n"
        f"- MCP recovery ID: `recovery:project:{slug}`\n"
    )


def _recovery_warning_item(slug: str, warning: str) -> ContextItem:
    from context_budget import ContextItem

    return ContextItem(
        item_id="handoff:recovery-status",
        text=warning.rstrip(),
        source=f"project:{slug}",
        priority=2,
        relevance=1.0,
        confidence="high",
        freshness="fresh",
        token_cost=len(warning.rstrip().encode("utf-8")),
        mandatory=True,
        representation="l1",
        parent_id=f"project:{slug}",
        priority_class="health",
    )


def _degraded_handoff(
    slug: str,
    items: tuple[ContextItem, ...],
    *,
    legacy: bool,
    max_chars: int,
    render_context: bool,
) -> ProjectHandoffResult:
    warning = _recovery_warning_text(slug)
    warning_item = _recovery_warning_item(slug, warning)
    if not render_context:
        return ProjectHandoffResult(
            "", degraded=True, legacy=legacy, items=(*items, warning_item)
        )
    handoff = _render_handoff_items(
        items, max_chars=max_chars - len(warning) - 2
    )
    return ProjectHandoffResult(
        handoff.rstrip() + "\n\n" + warning,
        degraded=True,
        legacy=legacy,
        items=(*items, warning_item),
    )


def recover_project_handoff(
    store: ProjectStore,
    slug: str,
    *,
    writer_wait_seconds: float = SESSION_START_RECOVERY_SECONDS,
    max_chars: int = MAX_PROJECT_HANDOFF_CHARS,
    project_root: Path | str | None = None,
    render_context: bool = True,
) -> ProjectHandoffResult:
    """Recover briefly, then render the last committed bounded project handoff."""
    degraded = False
    try:
        store.recover(slug, writer_wait_seconds=writer_wait_seconds)
    except TimeoutError:
        degraded = True
    projection, legacy = _legacy_or_current_projection(store, slug, project_root)
    items = build_handoff_items(projection)
    if degraded:
        return _degraded_handoff(
            slug,
            items,
            legacy=legacy,
            max_chars=max_chars,
            render_context=render_context,
        )
    return ProjectHandoffResult(
        _render_handoff_items(items, max_chars=max_chars) if render_context else "",
        legacy=legacy,
        items=items,
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("project lease times must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _require_journal_bytes(content: bytes) -> None:
    if not isinstance(content, bytes):
        raise TypeError("project journal content must be immutable bytes")
    if len(content) > MAX_JOURNAL_BYTES:
        raise ProjectJournalReadError(
            "too_large", f"project journal exceeds {MAX_JOURNAL_BYTES} bytes"
        )


def _journal_body(content: bytes) -> str:
    """The journal text with its verified header removed."""
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("project journal must be UTF-8") from exc
    if not text.startswith(JOURNAL_HEADER):
        raise ValueError("project journal header is invalid")
    return text.removeprefix(JOURNAL_HEADER)


def _journal_event_lines(content: bytes) -> list[str]:
    lines = [line for line in _journal_body(content).splitlines() if line]
    if len(lines) > MAX_JOURNAL_EVENTS:
        raise ProjectJournalReadError(
            "too_many_events",
            f"project journal exceeds {MAX_JOURNAL_EVENTS} event lines",
        )
    return lines


def _validated_journal_event(
    line: str, slug: str, previous: int
) -> dict[str, object]:
    event = json.loads(line)
    if canonical_json_bytes(event).decode("utf-8") != line:
        raise ValueError("project journal event is not canonical JSON")
    validate_schema(event, _SCHEMA)
    if event["project"] != slug or event["sequence"] <= previous:
        raise ValueError("project journal sequence or slug is invalid")
    return event


def parse_journal_events(slug: str, content: bytes) -> list[dict[str, object]]:
    """Validate and return canonical events from immutable journal bytes."""
    _require_slug(slug)
    _require_journal_bytes(content)
    if not content:
        return []
    events: list[dict[str, object]] = []
    previous = 0
    for line in _journal_event_lines(content):
        event = _validated_journal_event(line, slug, previous)
        previous = int(event["sequence"])
        events.append(event)
    return events


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


_RESERVED_WINDOWS_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)}
)


def _require_slug_identity(slug: str) -> None:
    if not isinstance(slug, str) or not slug or len(slug) > 256:
        raise ValueError("project slug must be a non-empty string up to 256 characters")
    if unicodedata.normalize("NFC", slug) != slug:
        raise ValueError("project slug must use NFC Unicode normalization")


def _require_slug_path_component(slug: str) -> None:
    if slug != slug.lower():
        raise ValueError("project slug must be lowercase to prevent case aliases")
    if slug in {".", ".."} or "/" in slug or "\\" in slug:
        raise ValueError("project slug must be one safe path component")


def _portable_slug_characters(slug: str) -> bool:
    return not any(
        character in '<>:"|?*'
        or character.isspace()
        or unicodedata.category(character).startswith("C")
        for character in slug
    )


def _require_unreserved_slug(slug: str) -> None:
    if slug.rstrip(" .").split(".", 1)[0].casefold() in _RESERVED_WINDOWS_NAMES:
        raise ValueError("project slug uses a reserved Windows name")


def _require_portable_slug(slug: str) -> None:
    if slug.endswith((" ", ".")):
        raise ValueError("project slug cannot end in a dot or space")
    if not _portable_slug_characters(slug):
        raise ValueError("project slug contains a non-portable character")
    _require_unreserved_slug(slug)


def _require_slug(slug: str) -> str:
    _require_slug_identity(slug)
    _require_slug_path_component(slug)
    _require_portable_slug(slug)
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


def _require_no_case_alias(projects: Path, slug: str) -> None:
    if not projects.is_dir():
        return
    key = unicodedata.normalize("NFC", slug).casefold()
    for entry in projects.iterdir():
        if unicodedata.normalize("NFC", entry.name).casefold() == key and entry.name != slug:
            raise ValueError("project slug collides with an existing case alias")


def _require_lease_ttl(ttl: object) -> None:
    if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= _HEARTBEAT_SECONDS:
        raise ValueError("project lease ttl must be an integer greater than 10 seconds")


def _require_lease_token(token: object) -> None:
    if token is not None and (not isinstance(token, str) or not token):
        raise ValueError("project lease token must be a non-empty string")


def _require_lease_holder(row: Mapping[str, object], owner: str, token: str | None, slug: str) -> None:
    if row["owner"] != owner or token is None or row["lease_token"] != token:
        raise ProjectLeaseBusy(f"project {slug!r} is leased by another invocation")


def _require_matching_ownership(ownership: object, slug: str) -> None:
    from operational_ownership import OwnerLease

    if not isinstance(ownership, OwnerLease):
        raise TypeError("ownership must be an OwnerLease")
    if ownership.role != "project" or ownership.scope != f"project:{slug}":
        raise ValueError("project ownership role or scope does not match")


def _scoped_query(query: str, slug: str | None) -> tuple[str, tuple[object, ...]]:
    """The query narrowed to one project, or left whole when no slug is named."""
    if slug is None:
        return query, ()
    return query + " AND project = ?", (slug,)


_RECOVERY_STATES = {
    "committed": "committed",
    "conflicted": "quarantined",
    "quarantined": "quarantined",
}


def _require_opened_regular(metadata: os.stat_result, label: str) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise ProjectJournalReadError(
            "not_regular", f"{label} is not a regular file"
        )


def _require_regular_file(metadata: os.stat_result, label: str) -> None:
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
        raise ProjectJournalReadError("unsafe_path", f"{label} is a link")
    _require_opened_regular(metadata, label)


def _require_bounded_size(size: int, max_bytes: int, label: str) -> None:
    if size > max_bytes:
        raise ProjectJournalReadError(
            "too_large", f"{label} exceeds {max_bytes} bytes"
        )


def _open_no_follow(path: Path, label: str) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        return os.open(path, flags)
    except (OSError, ValueError) as exc:
        raise ProjectJournalReadError(
            "unsafe_path", f"{label} cannot be opened safely"
        ) from exc


def _read_all(descriptor: int, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = max_bytes + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 1024 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_verified(
    descriptor: int, before: os.stat_result, max_bytes: int, label: str
) -> bytes:
    """Read the opened file, refusing bytes that changed under the descriptor."""
    opened = os.fstat(descriptor)
    if not _same_identity(before, opened):
        raise ProjectJournalReadError("changed", f"{label} changed before open")
    _require_opened_regular(opened, label)
    content = _read_all(descriptor, max_bytes)
    after = os.fstat(descriptor)
    if not _same_snapshot(opened, after) or len(content) != after.st_size:
        raise ProjectJournalReadError("changed", f"{label} changed while reading")
    _require_bounded_size(len(content), max_bytes, label)
    return content


def _require_traversable_directory(metadata: os.stat_result, label: str) -> None:
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
        raise ProjectJournalReadError(
            "unsafe_path", f"{label} parent traverses a link"
        )
    if not stat.S_ISDIR(metadata.st_mode):
        raise ProjectJournalReadError(
            "unsafe_path", f"{label} parent is not a directory"
        )


def _require_project_directory(metadata: os.stat_result) -> None:
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
        raise ProjectJournalReadError(
            "unsafe_path", "project directory traverses a link"
        )
    if not stat.S_ISDIR(metadata.st_mode):
        raise ProjectJournalReadError(
            "unsafe_path", "project path is not a directory"
        )


def _mkdir_if_missing(current: Path) -> os.stat_result:
    try:
        return os.lstat(current)
    except FileNotFoundError:
        pass
    try:
        os.mkdir(current)
    except FileExistsError:
        pass
    return os.lstat(current)


_SCALAR_FIELDS = ("goal", "phase", "current_task")
_PROJECTION_FIELDS = (*_SCALAR_FIELDS, *_MAX_LIST_ITEMS)


def _reduce_operation(target: dict[str, str], operation: Mapping[str, object]) -> None:
    item_id = str(operation["id"])
    if operation["action"] == "close":
        target.pop(item_id, None)
        return
    value = " ".join(str(operation["value"]).split())[:_MAX_VALUE_CHARS]
    target.pop(item_id, None)
    target[item_id] = value


def _projection_targets(projection: ProjectProjection) -> dict[str, dict[str, str]]:
    """The projection's item maps by name, shared by reference so writes land."""
    return {name: getattr(projection, name) for name in _PROJECTION_FIELDS}


def _apply_operations(target: dict[str, str], operations: object) -> None:
    assert isinstance(operations, list)
    for operation in operations:
        assert isinstance(operation, Mapping)
        _reduce_operation(target, operation)


def _apply_delta(
    targets: Mapping[str, dict[str, str]], delta: Mapping[str, object]
) -> None:
    """Reduce one event delta into the named item maps."""
    for name in _SCALAR_FIELDS:
        operation = delta[name]
        assert isinstance(operation, Mapping)
        _reduce_operation(targets[name], operation)
    for name in _SCALAR_FIELDS:
        _apply_operations(targets[name], delta.get(f"{name}_operations", []))
    for name in _MAX_LIST_ITEMS:
        _apply_operations(targets[name], delta[name])


def _legacy_context_or(delta: Mapping[str, object], current: str) -> str:
    context = delta.get("legacy_context")
    if isinstance(context, str) and context:
        return context
    return current


def _validate_event(event: Mapping[str, object], validated: bool) -> None:
    if not validated:
        validate_schema(event, _SCHEMA)


def _event_identity(event: Mapping[str, object]) -> tuple[str, str]:
    provenance = event["provenance"]
    assert isinstance(provenance, Mapping)
    return str(event["project"]), str(provenance["worktree"])


def _next_sequence(sequence: int, last_sequence: int) -> int:
    if sequence <= last_sequence:
        raise ValueError("project journal sequences must increase strictly")
    return sequence


def _reduce_events(
    ordered: Sequence[Mapping[str, object]], *, validated: bool
) -> tuple[dict[str, dict[str, str]], str, str, str, int]:
    """(item maps, project, project root, legacy context, last sequence)."""
    active: dict[str, dict[str, str]] = {name: {} for name in _PROJECTION_FIELDS}
    project = "project"
    project_root = ""
    legacy_context = ""
    last_sequence = 0
    for event in ordered:
        _validate_event(event, validated)
        project, project_root = _event_identity(event)
        last_sequence = _next_sequence(int(event["sequence"]), last_sequence)
        delta = event["delta"]
        assert isinstance(delta, Mapping)
        legacy_context = _legacy_context_or(delta, legacy_context)
        _apply_delta(active, delta)
    return active, project, project_root, legacy_context, last_sequence


_STATE_SECTIONS = (
    ("Goal", "goal"),
    ("Phase", "phase"),
    ("Current task", "current_task"),
    ("Next actions", "next_actions"),
    ("Recent decisions", "decisions"),
    ("Open blockers", "blockers"),
    ("Changed files", "changed_files"),
    ("Commands", "commands"),
    ("Verification", "verification"),
)


def _state_header_lines(project: str, project_root: str, last_sequence: int) -> list[str]:
    return [
        "---",
        "type: project-state",
        f"title: {_yaml_scalar(f'{project} - State')}",
        f"project: {_yaml_scalar(project)}",
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


def _state_section_lines(
    title: str, name: str, values: list[tuple[str, str]]
) -> list[str]:
    limit = 1 if name in _SCALAR_FIELDS else _MAX_LIST_ITEMS[name]
    kept = values[-limit:]
    if not kept:
        return [f"## {title}", "- None", ""]
    return [
        f"## {title}",
        *(f"- `{item_id}`: {value}" for item_id, value in kept),
        "",
    ]


def _legacy_value_kept(value: str) -> bool:
    return (
        bool(value)
        and value.lower() != "none"
        and not (value.startswith("<") and value.endswith(">"))
    )


def _legacy_section_values(body: str) -> list[str]:
    values = []
    for line in body.splitlines():
        value = re.sub(r"^-\s*(?:`[^`]+`:\s*)?", "", line).strip()
        if _legacy_value_kept(value):
            values.append(value[:4096])
    return values


def _legacy_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", text, re.MULTILINE))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1).strip().casefold()] = _legacy_section_values(
            text[match.end() : end]
        )
    return sections


_LEGACY_FIELD_TITLES = (
    ("goal", ("Goal", "Project goal")),
    ("phase", ("Phase", "Current phase")),
    ("task", ("Current task", "Current work")),
    ("next_actions", ("Next actions", "Next steps")),
    ("decisions", ("Recent decisions", "Decisions")),
    ("blockers", ("Open blockers", "Blockers", "Open threads")),
    ("changed_files", ("Changed files", "Files changed")),
    ("commands", ("Commands", "Commands run")),
    ("verification", ("Verification", "Test results")),
)

_LEGACY_MAPPED_TITLES = frozenset(
    title.casefold() for _, titles in _LEGACY_FIELD_TITLES for title in titles
)


def _legacy_fields(sections: Mapping[str, list[str]]) -> dict[str, list[str]]:
    return {
        name: [
            value for title in titles for value in sections.get(title.casefold(), [])
        ]
        for name, titles in _LEGACY_FIELD_TITLES
    }


def _legacy_context_section(title: str, values: list[str]) -> list[str]:
    if title in _LEGACY_MAPPED_TITLES:
        return []
    if title != "source":
        return values
    return [
        value
        for value in values
        if not re.match(r"Project root:", value, re.IGNORECASE)
    ]


def _legacy_summary_part(text: str) -> list[str]:
    summary = re.search(r"^One-sentence summary:\s*(.+)$", text, re.MULTILINE)
    if summary and not summary.group(1).strip().startswith("<"):
        return [f"Summary: {summary.group(1).strip()}"]
    return []


def _legacy_context_text(text: str, sections: Mapping[str, list[str]]) -> str:
    parts = _legacy_summary_part(text)
    for title, section_values in sections.items():
        kept = _legacy_context_section(title, section_values)
        if kept:
            parts.append(
                f"{title.title()}:\n" + "\n".join(f"- {value}" for value in kept)
            )
    return "\n\n".join(parts)[:16384]


def _owns_legacy_state(text: str, worktree: object) -> bool:
    """Whether this pre-journal state names the worktree the event came from."""
    owned_root = legacy_state_project_root(text)
    if owned_root is None or not isinstance(worktree, str):
        return False
    try:
        return Path(owned_root).resolve() == Path(worktree).resolve()
    except (OSError, ValueError):
        return owned_root == worktree


def _bootstrap_scalar(stable_hash: str, name: str, items: list[str]) -> dict[str, str]:
    if not items:
        return {"id": "checkpoint-none", "action": "close", "value": ""}
    value = items[-1]
    return {
        "id": _bootstrap_operation_id(stable_hash, name, 1, value),
        "action": "upsert",
        "value": value,
    }


def _bootstrap_operations(
    stable_hash: str, name: str, items: list[str], limit: int
) -> list[dict[str, str]]:
    return [
        {
            "id": _bootstrap_operation_id(stable_hash, name, index, value),
            "action": "upsert",
            "value": value,
        }
        for index, value in enumerate(items[:limit], 1)
    ]


def _bootstrap_delta(
    stable_hash: str, fields: Mapping[str, list[str]], legacy_context: str
) -> dict[str, object]:
    return {
        "goal": _bootstrap_scalar(stable_hash, "goal", fields["goal"]),
        "goal_operations": [],
        "phase": _bootstrap_scalar(stable_hash, "phase", fields["phase"]),
        "phase_operations": [],
        "current_task": _bootstrap_scalar(stable_hash, "task", fields["task"]),
        "current_task_operations": [],
        "next_actions": _bootstrap_operations(
            stable_hash, "next", fields["next_actions"], 10
        ),
        "decisions": _bootstrap_operations(
            stable_hash, "decision", fields["decisions"], 100
        ),
        "blockers": _bootstrap_operations(
            stable_hash, "blocker", fields["blockers"], 100
        ),
        "changed_files": _bootstrap_operations(
            stable_hash, "file", fields["changed_files"], 100
        ),
        "commands": _bootstrap_operations(
            stable_hash, "command", fields["commands"], 100
        ),
        "verification": _bootstrap_operations(
            stable_hash, "verify", fields["verification"], 100
        ),
        "legacy_context": legacy_context,
    }


def _bootstrap_event_from_state(
    slug: str, state: bytes, provenance: Mapping[str, object]
) -> dict[str, object] | None:
    text = state.decode("utf-8", errors="replace")
    if not _owns_legacy_state(text, provenance.get("worktree")):
        return None
    occurrence_id, idempotency_key, stable_hash = _bootstrap_event_identity(slug, text)
    sections = _legacy_sections(text)
    fields = _legacy_fields(sections)
    legacy_context = _legacy_context_text(text, sections)
    if not any((*fields.values(), legacy_context)):
        return None
    seed_provenance = dict(provenance)
    seed_provenance["source_event"] = f"bootstrap-state:{stable_hash}"
    return {
        "schema_version": "project-checkpoint/v1",
        "occurrence_id": occurrence_id,
        "idempotency_key": idempotency_key,
        "provenance": seed_provenance,
        "trigger": "legacy_state_bootstrap",
        "reason": "bootstrap_legacy_state",
        "delta": _bootstrap_delta(stable_hash, fields, legacy_context),
        "evidence_event_ids": [f"bootstrap-state:{stable_hash}"],
    }


def _legacy_probe_event(project_root: Path | str) -> dict[str, object]:
    return {
        "provenance": {
            "agent": "legacy-state",
            "session": "legacy-state",
            "worktree": str(project_root),
            "branch": "unknown",
            "source_event": "legacy-state",
        }
    }


def _project_lease_precondition(slug: str, lease: ProjectLease) -> dict[str, object]:
    return {
        "project": slug,
        "lease_token": lease.token,
        "fencing_epoch": lease.epoch,
        "expires_at": _timestamp(lease.expires_at),
    }


def _require_bound_event(existing: Mapping[str, object], event: object) -> None:
    if canonical_json_bytes(existing) != canonical_json_bytes(event):
        raise ValueError("project journal sequence is bound to another event")


def _require_journal_bounds(records: list[object], journal: bytes) -> None:
    if len(records) > MAX_JOURNAL_EVENTS:
        raise ProjectJournalReadError(
            "too_many_events",
            f"project journal exceeds {MAX_JOURNAL_EVENTS} event lines",
        )
    if len(journal) > MAX_JOURNAL_BYTES:
        raise ProjectJournalReadError(
            "too_large", f"project journal exceeds {MAX_JOURNAL_BYTES} bytes"
        )


def _appended_journal(current_journal: bytes, event: object, records: list) -> bytes:
    journal = current_journal or JOURNAL_HEADER.encode("utf-8")
    if not journal.endswith(b"\n"):
        raise ValueError("project journal must end with a newline")
    records.append(event)
    return journal + canonical_json_bytes(event) + b"\n"


# A full journal used to be the end of the project's record. Appending the next
# event was refused by `_require_journal_bounds` and nothing rotated, so the
# refusal was permanent.
#
# Measured 2026-08-29 on the live vault: `knowledge/projects/llm-wiki/journal.md`
# held exactly MAX_JOURNAL_EVENTS events — reading it still worked, appending
# was refused — and 4 698 checkpoint events waited behind that refusal while
# `run/state.json` grew to 10 MB and the same failure repeated every ten seconds
# into a log no health check read.
#
# It cannot be trimmed: the projection is folded from the first event, so
# dropping an event drops the state it carried. It is sealed instead, which is
# the move every append-only store makes — the sealed segment is immutable and
# stays on disk, and the fresh journal opens with one event restating the fold
# of everything sealed, so the same projection comes back from the live segment
# alone.
_ROTATION_ID = "journal-rotation"


def _sealed_segment_path(slug: str, records: list) -> str:
    """Named by the range it holds, so history is findable without an index."""
    first = int(records[0]["sequence"])
    last = int(records[-1]["sequence"])
    return f"knowledge/projects/{slug}/journal.{first:06d}-{last:06d}.md"


def _snapshot_operations(items: Mapping[str, str]) -> list[dict[str, str]]:
    return [
        {"id": item_id, "action": "upsert", "value": value}
        for item_id, value in items.items()
    ]


def _snapshot_delta(active: Mapping[str, dict[str, str]], context: str) -> dict:
    """One delta that reproduces the fold of every sealed event.

    The scalar slot carries a close of an id nothing holds, so it removes
    nothing, and the surviving ids follow in `<name>_operations`. That is the
    only shape the schema offers for a scalar that holds more than one id.
    """
    delta: dict[str, object] = {"legacy_context": context}
    for name in _SCALAR_FIELDS:
        delta[name] = {"id": _ROTATION_ID, "action": "close", "value": ""}
        delta[f"{name}_operations"] = _snapshot_operations(active[name])
    for name in _MAX_LIST_ITEMS:
        delta[name] = _snapshot_operations(active[name])
    return delta


def _sealed_worktree(records: list) -> str:
    """The project root the sealed events last reported."""
    provenance = records[-1]["provenance"]
    assert isinstance(provenance, Mapping)
    return str(provenance["worktree"])


def _snapshot_event(slug: str, records: list, segment: str) -> dict[str, object]:
    """The sealed journal's fold, restated as the fresh journal's first event.

    It carries the sealed head's own sequence rather than a new one: the
    coordinator allocates sequences and a synthetic one would desynchronise the
    head. Restating that sequence is also the honest reading — this is the state
    as of that event, not an event after it.
    """
    active, _project, _root, context, last = _reduce_events(records, validated=True)
    return {
        "schema_version": "project-checkpoint/v1",
        "occurrence_id": f"{_ROTATION_ID}:{slug}:{last}",
        "idempotency_key": f"{_ROTATION_ID}:{slug}:{last}",
        "project": slug,
        "sequence": last,
        # The worktree is the project root the projection reports, so it is
        # carried from the sealed head rather than invented: a snapshot that
        # named itself as the root rewrote the project's own location. Caught
        # by `test_rotation_does_not_change_the_projection` before it shipped.
        "provenance": {
            "agent": _ROTATION_ID,
            "session": _ROTATION_ID,
            "worktree": _sealed_worktree(records),
            "branch": _ROTATION_ID,
            "source_event": f"{_ROTATION_ID}:{last}",
        },
        "trigger": "journal_rotation",
        "reason": f"sealed {len(records)} events into {segment}",
        "delta": _snapshot_delta(active, context),
        "evidence_event_ids": [],
        "last_applied_sequence": last,
    }


def _rotated_journal(slug: str, records: list, current_journal: bytes):
    """Seal a full journal and open a fresh one. None when it is not full."""
    if len(records) < MAX_JOURNAL_EVENTS:
        return None
    segment = _sealed_segment_path(slug, records)
    snapshot = _snapshot_event(slug, records, segment)
    journal = (
        JOURNAL_HEADER.encode("utf-8") + canonical_json_bytes(snapshot) + b"\n"
    )
    return segment, current_journal, journal, [snapshot]


def _require_journal_head(slug: str, sequence: int, records: list) -> None:
    journal_head = int(records[-1]["sequence"]) if records else 0
    if journal_head != sequence - 1:
        raise ProjectJournalRebuildRequired(slug, sequence, journal_head)


def _extended_journal(
    slug: str, sequence: int, event: object, records: list, current_journal: bytes
):
    """The journal bytes to write, and the segment to seal when one is due.

    Returns `(journal, sealed)`, where `sealed` is `None` or the path and bytes
    of the segment the caller must create in the same transaction.
    """
    matching = [item for item in records if item["sequence"] == sequence]
    if matching:
        _require_bound_event(matching[0], event)
        _require_journal_bounds(records, current_journal)
        return current_journal, None
    _require_journal_head(slug, sequence, records)
    rotation = _rotated_journal(slug, records, current_journal)
    if rotation is not None:
        segment, sealed_bytes, current_journal, records[:] = rotation
        journal = _appended_journal(current_journal, event, records)
        _require_journal_bounds(records, journal)
        return journal, (segment, sealed_bytes)
    journal = _appended_journal(current_journal, event, records)
    _require_journal_bounds(records, journal)
    return journal, None


def _markdown_change(
    path: str, content: bytes, max_before: int, exists: bool
) -> MarkdownChange:
    if exists:
        return MarkdownChange.replace(path, content, max_before_bytes=max_before)
    return MarkdownChange.create(path, content, max_before_bytes=max_before)


def _sealed_change(sealed) -> list[MarkdownChange]:
    """The immutable segment, created in the same transaction as the fresh journal.

    Create, never replace: a sealed segment that could be overwritten would not
    be sealed, and the transaction's absent-precondition proves nothing already
    holds that range.
    """
    if sealed is None:
        return []
    path, content = sealed
    return [MarkdownChange.create(path, content, max_before_bytes=MAX_JOURNAL_BYTES)]


def _checkpoint_changes(
    slug: str,
    journal: bytes,
    state: bytes,
    current_journal: bytes,
    current_state: bytes | None,
    sealed=None,
) -> list[MarkdownChange]:
    return [
        *_sealed_change(sealed),
        _markdown_change(
            f"knowledge/projects/{slug}/journal.md",
            journal,
            MAX_JOURNAL_BYTES,
            bool(current_journal),
        ),
        _markdown_change(
            f"knowledge/projects/{slug}/state.md",
            state,
            MAX_PROJECTION_BYTES,
            current_state is not None,
        ),
    ]


def _hash_or_absent(content: bytes | None, present: bool) -> object:
    return sha256_bytes(content) if present else ABSENT


def _sealed_precondition(sealed) -> dict[str, object]:
    """Nothing may already hold this range, or the seal would overwrite history."""
    if sealed is None:
        return {}
    path, _content = sealed
    return {path: ABSENT}


def _checkpoint_preconditions(
    slug: str,
    lease: ProjectLease,
    *,
    current_journal: bytes,
    current_state: bytes | None,
    sealed=None,
) -> dict[str, object]:
    return {
        **_sealed_precondition(sealed),
        "project_lease": _project_lease_precondition(slug, lease),
        f"knowledge/projects/{slug}/journal.md": _hash_or_absent(
            current_journal, bool(current_journal)
        ),
        f"knowledge/projects/{slug}/state.md": _hash_or_absent(
            current_state, current_state is not None
        ),
    }


def _apply_transaction(
    coordinator: MarkdownCoordinator,
    transaction_id: str,
    writer_wait_seconds: float | None,
):
    if writer_wait_seconds is None:
        return coordinator.apply(transaction_id)
    return coordinator.apply(transaction_id, writer_wait_seconds=writer_wait_seconds)


def _committed_reservation(
    row: ProjectCheckpointReservation, transaction_id: str
) -> ProjectCheckpointReservation:
    return ProjectCheckpointReservation(
        project=row.project,
        sequence=row.sequence,
        occurrence_id=row.occurrence_id,
        idempotency_key=row.idempotency_key,
        event_json=row.event_json,
        operation_id=row.operation_id,
        attempt_number=row.attempt_number,
        state="committed",
        transaction_id=transaction_id,
        parent_operation_id=row.parent_operation_id,
        duplicate=row.duplicate,
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
        # Adoption tombstones the pre-adoption coordinator database, so asking
        # for the coordinator by rule is the only way this writer survives it.
        self.coordinator = active_or_legacy_coordinator(vault, state_root)
        self.vault = self.coordinator.vault
        self.state_root = self.coordinator.state_root
        self._clock = clock

    @classmethod
    def _from_v3_candidate(
        cls,
        vault: Path,
        *,
        state_root: Path,
        clock: Callable[[], datetime] = _utc_now,
    ) -> ProjectStore:
        candidate = Path(state_root) / "run/markdown-transactions-v3.candidate.sqlite3"
        store = cls.__new__(cls)
        store.coordinator = MarkdownCoordinator._from_v3_candidate(
            candidate, state_root=Path(state_root)
        )
        store.vault = Path(vault).resolve(strict=True)
        store.coordinator.vault = store.vault
        store.state_root = Path(state_root)
        store._clock = clock
        return store

    def _project_directory(self, slug: str) -> Path:
        slug = _require_slug(slug)
        projects = self.vault / "knowledge" / "projects"
        target = projects / slug
        try:
            target.relative_to(projects)
        except ValueError as exc:
            raise ValueError("project slug escapes the projects directory") from exc
        _require_no_case_alias(projects, slug)
        return target

    def acquire_lease(
        self,
        slug: str,
        owner: str,
        ttl: int = 30,
        *,
        token: str | None = None,
        now: datetime | None = None,
        ownership: OwnerLease | None = None,
    ) -> ProjectLease:
        slug = _require_slug(slug)
        self._project_directory(slug)
        owner = _require_owner(owner)
        _require_lease_ttl(ttl)
        _require_lease_token(token)
        if getattr(self.coordinator, "_database_contract", None) is not None:
            return self._acquire_v3_lease(
                slug,
                owner,
                ttl,
                token=token,
                now=now,
                ownership=ownership,
            )
        return self._acquire_legacy_lease(slug, owner, ttl, token=token, now=now)

    def _acquire_legacy_lease(
        self,
        slug: str,
        owner: str,
        ttl: int,
        *,
        token: str | None,
        now: datetime | None,
    ) -> ProjectLease:
        current_time = now or self._clock()
        expires_at = current_time + timedelta(seconds=ttl)
        with self.coordinator._connect() as database, begin_immediate(database):
            row = database.execute(
                "SELECT * FROM project_leases WHERE project = ?", (slug,)
            ).fetchone()
            if row is not None and _parse_timestamp(row["expires_at"]) > current_time:
                return self._renew_legacy_lease(
                    database,
                    row,
                    slug=slug,
                    owner=owner,
                    token=token,
                    current_time=current_time,
                    expires_at=expires_at,
                )
            if token is not None:
                raise ProjectFenceError("project lease token is stale or expired")
            token, epoch = self._insert_legacy_lease(
                database,
                row,
                slug=slug,
                owner=owner,
                current_time=current_time,
                expires_at=expires_at,
            )
        return ProjectLease(slug, owner, token, epoch, expires_at, current_time)

    @staticmethod
    def _insert_legacy_lease(
        database,
        row,
        *,
        slug: str,
        owner: str,
        current_time: datetime,
        expires_at: datetime,
    ) -> tuple[str, int]:
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
        return token, epoch

    @staticmethod
    def _renew_legacy_lease(
        database,
        row,
        *,
        slug: str,
        owner: str,
        token: str | None,
        current_time: datetime,
        expires_at: datetime,
    ) -> ProjectLease:
        _require_lease_holder(row, owner, token, slug)
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

    def _ownership_registry(self, clock: Callable[[], datetime] | None = None):
        """The canonical ownership registry of whichever coordinator is open.

        `OwnershipRegistry(state_root)` addresses the migration candidate
        database, which an adopted vault no longer has. Once adoption is in
        force the registry has to be opened on the coordinator this store is
        actually using, exactly as `MarkdownCoordinator._ownership_registry`
        does.
        """
        from operational_ownership import OwnershipRegistry, utc_now

        effective = utc_now if clock is None else clock
        if getattr(self.coordinator, "_database_contract", None) is None:
            return OwnershipRegistry(self.state_root, clock=effective)
        return OwnershipRegistry._from_adopted_database(
            self.state_root, self.coordinator.database_path, clock=effective
        )

    def _acquire_v3_lease(
        self,
        slug: str,
        owner: str,
        ttl: int,
        *,
        token: str | None,
        now: datetime | None,
        ownership: OwnerLease | None,
    ) -> ProjectLease:
        current_time = now or self._clock()
        registry = self._ownership_registry(lambda: current_time)
        with self.coordinator._connect() as database, begin_immediate(database):
            renewed = (
                self._renew_v3_lease(
                    database, registry, slug, owner, ttl, token=token, now=current_time
                )
                if ownership is None
                else None
            )
            if renewed is not None:
                return renewed
            canonical = self._canonical_ownership(
                database, registry, slug, token=token, ownership=ownership
            )
            expires_at = current_time + timedelta(seconds=ttl)
            lease = ProjectLease(
                slug,
                owner,
                canonical.token,
                canonical.epoch,
                expires_at,
                current_time,
                canonical,
                ownership is None,
            )
            self._insert_project_projection(database, lease, canonical)
        return lease

    def _renew_v3_lease(
        self,
        database,
        registry,
        slug: str,
        owner: str,
        ttl: int,
        *,
        token: str | None,
        now: datetime,
    ) -> ProjectLease | None:
        """Renew the unexpired canonical lease, or None when there is none to renew."""
        row = database.execute(
            "SELECT * FROM project_leases WHERE project=?", (slug,)
        ).fetchone()
        if row is None or _parse_timestamp(row["expires_at"]) <= now:
            return None
        _require_lease_holder(row, owner, token, slug)
        canonical = registry._heartbeat_in_transaction(
            database, self._canonical_owner_lease(database, slug)
        )
        expires_at = now + timedelta(seconds=ttl)
        self._refresh_v3_projection(
            database, slug, token, row["fencing_epoch"], expires_at, now
        )
        return ProjectLease(
            slug,
            owner,
            canonical.token,
            canonical.epoch,
            expires_at,
            now,
            canonical,
            True,
        )

    def _canonical_owner_lease(self, database, slug: str):
        from operational_ownership import OwnerLease, ProcessIdentity

        existing = database.execute(
            "SELECT * FROM maintenance_owners WHERE role='project' AND scope=?",
            (f"project:{slug}",),
        ).fetchone()
        if existing is None:
            raise ProjectFenceError("project canonical lease is absent")
        return OwnerLease(
            state_root=self.state_root,
            role="project",
            scope=f"project:{slug}",
            actor_id=existing["actor_id"],
            token=existing["owner_token"],
            epoch=existing["fencing_epoch"],
            process=ProcessIdentity(
                pid=existing["process_id"],
                start_identity=existing["process_start_identity"],
            ),
            acquired_at=_parse_timestamp(existing["acquired_at"]),
            heartbeat_at=_parse_timestamp(existing["heartbeat_at"]),
            expires_at=_parse_timestamp(existing["expires_at"]),
            ttl_seconds=30,
            heartbeat_seconds=10,
        )

    @staticmethod
    def _refresh_v3_projection(
        database,
        slug: str,
        token: str | None,
        epoch: int,
        expires_at: datetime,
        now: datetime,
    ) -> None:
        updated = database.execute(
            "UPDATE project_leases SET expires_at=?,heartbeat_at=? "
            "WHERE project=? AND lease_token=? AND fencing_epoch=?",
            (_timestamp(expires_at), _timestamp(now), slug, token, epoch),
        ).rowcount
        if updated != 1:
            raise ProjectFenceError("project lease is stale or expired")

    def _canonical_ownership(
        self,
        database,
        registry,
        slug: str,
        *,
        token: str | None,
        ownership: OwnerLease | None,
    ):
        if ownership is None:
            if token is not None:
                raise ProjectFenceError("project lease token is stale or expired")
            return registry._acquire_in_transaction(
                database, "project", scope=f"project:{slug}"
            )
        _require_matching_ownership(ownership, slug)
        registry.require(database, ownership)
        return ownership

    @staticmethod
    def _insert_project_projection(database, lease: ProjectLease, ownership) -> None:
        database.execute(
            """INSERT INTO project_leases(
                   project,lease_token,fencing_epoch,owner,expires_at,heartbeat_at,
                   canonical_role,canonical_scope,actor_id,process_id,
                   process_start_identity
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                lease.slug,
                lease.token,
                lease.epoch,
                lease.owner,
                _timestamp(lease.expires_at),
                _timestamp(lease.heartbeat_at),
                ownership.role,
                ownership.scope,
                ownership.actor_id,
                ownership.process.pid,
                ownership.process.start_identity,
            ),
        )

    def heartbeat(
        self,
        lease: ProjectLease,
        ttl: int = 30,
        *,
        now: datetime | None = None,
    ) -> ProjectLease:
        if not isinstance(lease, ProjectLease):
            raise TypeError("lease must be a ProjectLease")
        _require_lease_ttl(ttl)
        current_time = now or self._clock()
        expires_at = current_time + timedelta(seconds=ttl)
        if getattr(self.coordinator, "_database_contract", None) is not None:
            return self._heartbeat_v3(lease, current_time, expires_at)
        return self._heartbeat_legacy(lease, current_time, expires_at)

    def _heartbeat_v3(
        self, lease: ProjectLease, current_time: datetime, expires_at: datetime
    ) -> ProjectLease:
        if lease._ownership is None:
            raise ProjectFenceError("project lease has no canonical ownership")
        registry = self._ownership_registry(lambda: current_time)
        with self.coordinator._connect() as database, begin_immediate(database):
            canonical = registry._heartbeat_in_transaction(
                database, lease._ownership
            )
            updated = database.execute(
                "UPDATE project_leases SET expires_at=?, heartbeat_at=? "
                "WHERE project=? AND lease_token=? AND fencing_epoch=?",
                (
                    _timestamp(expires_at),
                    _timestamp(current_time),
                    lease.slug,
                    lease.token,
                    lease.epoch,
                ),
            ).rowcount
            if updated != 1:
                raise ProjectFenceError("project lease is stale or expired")
        return replace(
            lease,
            expires_at=expires_at,
            heartbeat_at=current_time,
            _ownership=canonical,
        )

    def _heartbeat_legacy(
        self, lease: ProjectLease, current_time: datetime, expires_at: datetime
    ) -> ProjectLease:
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
        self._bootstrap_legacy_state(
            slug, normalized_event, owner, writer_wait_seconds
        )
        lease = self.acquire_lease(slug, owner)
        try:
            return self._checkpoint_under_lease(
                slug, normalized_event, lease, writer_wait_seconds
            )
        finally:
            self._release(lease)

    def _bootstrap_legacy_state(
        self,
        slug: str,
        normalized_event: Mapping[str, object],
        owner: str,
        writer_wait_seconds: float | None,
    ) -> None:
        """Lift an owned pre-journal state into its own checkpoint first."""
        if normalized_event.get("trigger") == "legacy_state_bootstrap":
            return
        bootstrap = self._legacy_bootstrap_event(slug, normalized_event)
        if bootstrap is None:
            return
        self.checkpoint(
            slug,
            bootstrap,
            owner,
            writer_wait_seconds=writer_wait_seconds,
        )

    def _checkpoint_under_lease(
        self,
        slug: str,
        event: Mapping[str, object],
        lease: ProjectLease,
        writer_wait_seconds: float | None,
    ) -> CheckpointReceipt:
        reserved, duplicate = self._reserve(slug, event, lease)
        if duplicate and reserved.state == "committed":
            return self._receipt(reserved, duplicate=True)
        try:
            return self._apply_reserved(reserved, lease, writer_wait_seconds)
        except ProjectJournalRebuildRequired:
            self._set_checkpoint_state(slug, reserved.sequence, "quarantined")
            raise
        except TransactionFailure as exc:
            self._quarantine_precondition_failure(exc, slug, reserved.sequence)
            raise

    def _apply_reserved(
        self,
        reserved: ProjectCheckpointReservation,
        lease: ProjectLease,
        writer_wait_seconds: float | None,
    ) -> CheckpointReceipt:
        if writer_wait_seconds is None:
            return self._project_reserved(reserved, lease)
        return self._project_reserved(
            reserved, lease, writer_wait_seconds=writer_wait_seconds
        )

    def _quarantine_precondition_failure(
        self, exc: TransactionFailure, slug: str, sequence: int
    ) -> None:
        if exc.code != "precondition_failed":
            return
        self._set_checkpoint_state(slug, sequence, "quarantined")
        raise ProjectFenceError(
            "project lease changed before checkpoint apply"
        ) from exc

    def _legacy_bootstrap_event(
        self, slug: str, event: Mapping[str, object]
    ) -> dict[str, object] | None:
        if self._read_journal_bytes(slug):
            return None
        state = self._read_projection_bytes(slug)
        provenance = event.get("provenance")
        if state is None or not isinstance(provenance, Mapping):
            return None
        return _bootstrap_event_from_state(slug, state, provenance)

    def legacy_projection(
        self, slug: str, project_root: Path | str
    ) -> ProjectProjection | None:
        """Parse an owned pre-journal state without mutating it."""
        event = self._legacy_bootstrap_event(slug, _legacy_probe_event(project_root))
        if event is None:
            return None
        delta = event["delta"]
        assert isinstance(delta, Mapping)
        projection = ProjectProjection(project=slug)
        _apply_delta(_projection_targets(projection), delta)
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
        self._require_recover_scope(slug)
        candidates = self._pending_candidates(slug)
        records = self._transaction_records(writer_wait_seconds)
        recovered, replay = self._settle_pending(slug, candidates, records)
        recovered.extend(self._replay_reservations(replay, writer_wait_seconds))
        return recovered

    def _require_recover_scope(self, slug: str | None) -> None:
        if slug is None:
            return
        _require_slug(slug)
        self._project_directory(slug)

    def _pending_candidates(self, slug: str | None) -> set[tuple[str, int]]:
        text, parameters = _scoped_query(
            "SELECT project, sequence FROM project_checkpoints "
            "WHERE state != 'committed'",
            slug,
        )
        with self.coordinator._connect() as database:
            return {
                (row["project"], row["sequence"])
                for row in database.execute(text, parameters)
            }

    def _transaction_records(self, writer_wait_seconds: float | None) -> dict:
        transaction_records = (
            self.coordinator.recover()
            if writer_wait_seconds is None
            else self.coordinator.recover(
                writer_wait_seconds=writer_wait_seconds
            )
        )
        return {record.id: record for record in transaction_records}

    def _settle_pending(
        self,
        slug: str | None,
        candidates: set[tuple[str, int]],
        records: dict,
    ) -> tuple[list[CheckpointReceipt], list[ProjectCheckpointReservation]]:
        """Settle every non-committed checkpoint against its transaction record."""
        recovered: list[CheckpointReceipt] = []
        replay: list[ProjectCheckpointReservation] = []
        text, parameters = _scoped_query(
            "SELECT * FROM project_checkpoints WHERE state IN ('prepared', 'reserved')",
            slug,
        )
        with self.coordinator._connect() as database, begin_immediate(database):
            recovered.extend(self._committed_receipts(database, candidates))
            rows = list(
                database.execute(text + " ORDER BY project, sequence", parameters)
            )
            for row in rows:
                self._settle_row(database, row, records, candidates, recovered, replay)
        return recovered, replay

    def _committed_receipts(
        self, database, candidates: set[tuple[str, int]]
    ) -> list[CheckpointReceipt]:
        receipts: list[CheckpointReceipt] = []
        for project, sequence in sorted(candidates):
            committed = database.execute(
                "SELECT * FROM project_checkpoints WHERE project = ? "
                "AND sequence = ? AND state = 'committed'",
                (project, sequence),
            ).fetchone()
            if committed is not None:
                receipts.append(self._receipt(committed))
        return receipts

    def _settle_row(
        self,
        database,
        row,
        records: dict,
        candidates: set[tuple[str, int]],
        recovered: list[CheckpointReceipt],
        replay: list[ProjectCheckpointReservation],
    ) -> None:
        record = self._settled_record(row, records)
        if record is None:
            replay.append(self.coordinator._project_reservation(row))
            return
        state = _RECOVERY_STATES.get(record.state)
        if state is not None:
            self._mark_settled(database, row, state, candidates, recovered)

    def _settled_record(self, row, records: dict):
        transaction_id = row["transaction_id"]
        record = records.get(transaction_id)
        if record is None and transaction_id:
            return self.coordinator._record(transaction_id)
        return record

    def _mark_settled(
        self,
        database,
        row,
        state: str,
        candidates: set[tuple[str, int]],
        recovered: list[CheckpointReceipt],
    ) -> None:
        self._mark_checkpoint(database, row, state)
        if state == "committed" and (row["project"], row["sequence"]) in candidates:
            recovered.append(self._receipt(row))

    @staticmethod
    def _mark_checkpoint(database, row, state: str) -> None:
        database.execute(
            "UPDATE project_checkpoints SET state = ? "
            "WHERE project = ? AND sequence = ?",
            (state, row["project"], row["sequence"]),
        )
        database.execute(
            "UPDATE project_checkpoint_attempts SET state = ? "
            "WHERE transaction_id = ?",
            (state, row["transaction_id"]),
        )

    def _replay_reservations(
        self,
        replay: list[ProjectCheckpointReservation],
        writer_wait_seconds: float | None,
    ) -> list[CheckpointReceipt]:
        recovered: list[CheckpointReceipt] = []
        for row in replay:
            receipt = self._replay_one(row, writer_wait_seconds)
            if receipt is not None:
                recovered.append(receipt)
        return recovered

    def _replay_one(
        self,
        row: ProjectCheckpointReservation,
        writer_wait_seconds: float | None,
    ) -> CheckpointReceipt | None:
        try:
            lease = self.acquire_lease(row.project, "project-recovery")
        except ProjectLeaseBusy:
            return None
        try:
            return self._replayed_under_lease(row, lease, writer_wait_seconds)
        finally:
            self._release(lease)

    def _replayed_under_lease(
        self,
        row: ProjectCheckpointReservation,
        lease: ProjectLease,
        writer_wait_seconds: float | None,
    ) -> CheckpointReceipt | None:
        try:
            return self._project_reserved(
                row, lease, writer_wait_seconds=writer_wait_seconds
            )
        except ProjectPendingPriorError:
            return None
        except TransactionFailure as exc:
            if exc.code != "precondition_failed":
                raise
            self._set_checkpoint_state(row.project, row.sequence, "quarantined")
            return None

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
        _require_regular_file(before, label)
        _require_bounded_size(before.st_size, max_bytes, label)
        descriptor = _open_no_follow(path, label)
        try:
            return _read_verified(descriptor, before, max_bytes, label)
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
            _require_traversable_directory(metadata, label)

    def _ensure_project_directory(self, slug: str) -> None:
        target = self._project_directory(slug)
        relative = target.relative_to(self.vault)
        current = self.vault
        for part in relative.parts:
            current = current / part
            _require_project_directory(_mkdir_if_missing(current))

    def render_state(
        self,
        events: Sequence[Mapping[str, object]],
        *,
        _validated: bool = False,
    ) -> bytes:
        ordered = sorted(events, key=lambda item: int(item["sequence"]))
        active, project, project_root, legacy_context, last_sequence = _reduce_events(
            ordered, validated=_validated
        )
        lines = _state_header_lines(project, project_root, last_sequence)
        for title, name in _STATE_SECTIONS:
            lines.extend(
                _state_section_lines(title, name, list(active[name].items()))
            )
        lines.append("## Legacy context")
        lines.append(legacy_context or "- None")
        lines.append("")
        return ("\n".join(lines).rstrip() + "\n").encode("utf-8")

    def projection(self, slug: str) -> ProjectProjection:
        """Reduce the current bounded journal into an in-memory projection."""
        records = self._journal_events(slug, self._read_journal_bytes(slug))
        active = ProjectProjection(project=slug)
        targets = _projection_targets(active)
        for event in records:
            active.project = str(event["project"])
            active.last_applied_sequence = int(event["sequence"])
            delta = event["delta"]
            assert isinstance(delta, Mapping)
            active.legacy_context = _legacy_context_or(delta, active.legacy_context)
            _apply_delta(targets, delta)
        return active

    @staticmethod
    def _reduce(target: dict[str, str], operation: Mapping[str, object]) -> None:
        _reduce_operation(target, operation)

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
        event = json.loads(row.event_json)
        with self.coordinator._connect() as database:
            self.coordinator._check_project_head(database, slug, row.sequence)
        lease = self.heartbeat(lease)
        current_journal = self._read_journal_bytes(slug)
        records = self._journal_events(slug, current_journal)
        lease = self.heartbeat(lease)
        journal, sealed = _extended_journal(
            slug, row.sequence, event, records, current_journal
        )
        lease = self.heartbeat(lease)
        state = self.render_state(records, _validated=True)
        lease = self.heartbeat(lease)
        current_state = self._read_projection_bytes(slug)
        lease = self.heartbeat(lease)
        self._ensure_project_directory(slug)
        return self._committed_checkpoint(
            row,
            lease,
            _checkpoint_changes(
                slug, journal, state, current_journal, current_state, sealed
            ),
            _checkpoint_preconditions(
                slug,
                lease,
                current_journal=current_journal,
                current_state=current_state,
                sealed=sealed,
            ),
            writer_wait_seconds,
        )

    def _checkpoint_writer_gate(self, lease: ProjectLease):
        """The gate this store already owns, or none at all before adoption.

        The canonical registry allows one owner row per actor, and a v3 project
        lease is that row. Letting `prepare` claim a second `markdown-writer`
        row fails with `owner_identity_conflict`, so the adopted path lends the
        gate the owner it already holds, exactly as `mutate_owned_knowledge`
        does for the capture worker. Before adoption there is no canonical
        owner and `prepare` claims the legacy gate itself, as it always did.
        """
        if lease._ownership is None:
            return contextlib.nullcontext()
        return self.coordinator.writer_gate(owner=lease._ownership)

    def _committed_checkpoint(
        self,
        row: ProjectCheckpointReservation,
        lease: ProjectLease,
        changes: list[MarkdownChange],
        preconditions: dict[str, object],
        writer_wait_seconds: float | None,
    ) -> CheckpointReceipt:
        with self._checkpoint_writer_gate(lease):
            transaction = self.coordinator.prepare(
                changes,
                operation_id=row.operation_id,
                preconditions=preconditions,
                project_reservation=row,
            )
            return self._apply_checkpoint(
                row, lease, transaction, writer_wait_seconds
            )

    def _apply_checkpoint(
        self,
        row: ProjectCheckpointReservation,
        lease: ProjectLease,
        transaction,
        writer_wait_seconds: float | None,
    ) -> CheckpointReceipt:
        # The lease precondition is refreshed twice on purpose: each refresh
        # narrows the window between the last heartbeat and the apply.
        lease = self._refreshed_lease_precondition(row.project, lease, transaction.id)
        lease = self._refreshed_lease_precondition(row.project, lease, transaction.id)
        committed = _apply_transaction(
            self.coordinator, transaction.id, writer_wait_seconds
        )
        if committed.state != "committed":
            raise RuntimeError("project checkpoint transaction did not commit")
        return self._receipt(_committed_reservation(row, transaction.id))

    def _refreshed_lease_precondition(
        self, slug: str, lease: ProjectLease, transaction_id: str
    ) -> ProjectLease:
        lease = self.heartbeat(lease)
        self.coordinator.refresh_project_lease_precondition(
            transaction_id, _project_lease_precondition(slug, lease)
        )
        return lease

    def _journal_events(self, slug: str, content: bytes) -> list[dict[str, object]]:
        return parse_journal_events(slug, content)

    def _release(self, lease: ProjectLease) -> None:
        now = self._clock()
        with self.coordinator._connect() as database, begin_immediate(database):
            if getattr(self.coordinator, "_database_contract", None) is not None:
                self._release_v3(database, lease)
                return
            database.execute(
                "UPDATE project_leases SET expires_at = ? WHERE project = ? "
                "AND lease_token = ? AND fencing_epoch = ?",
                (_timestamp(now), lease.slug, lease.token, lease.epoch),
            )

    def _release_v3(self, database, lease: ProjectLease) -> None:
        row = database.execute(
            "SELECT 1 FROM maintenance_owners WHERE role='project' AND scope=?",
            (f"project:{lease.slug}",),
        ).fetchone()
        deleted = database.execute(
            "DELETE FROM project_leases WHERE project=? AND lease_token=? "
            "AND fencing_epoch=?",
            (lease.slug, lease.token, lease.epoch),
        ).rowcount
        if deleted != 1 or row is None:
            raise ProjectFenceError("project lease is stale or expired")
        if lease._release_canonical:
            self._release_canonical_owner(database, lease)

    def _release_canonical_owner(self, database, lease: ProjectLease) -> None:
        if lease._ownership is None:
            raise ProjectFenceError("project lease has no canonical ownership")
        self._ownership_registry()._release_in_transaction(
            database, lease._ownership
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
