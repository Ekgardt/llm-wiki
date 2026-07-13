"""Fenced append-only project journals and deterministic state projections."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_HEARTBEAT_SECONDS = 10
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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("project lease times must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _require_slug(slug: str) -> str:
    if not isinstance(slug, str) or _SLUG.fullmatch(slug) is None:
        raise ValueError("project slug must match ^[a-z0-9][a-z0-9-]*$")
    return slug


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
        if current_time < lease.heartbeat_due_at:
            raise ValueError("project lease heartbeat is not due")
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
    ) -> CheckpointReceipt:
        slug = _require_slug(slug)
        _require_owner(owner)
        normalized_event = self.coordinator.normalize_project_checkpoint(slug, event)
        self.recover(slug)
        lease = self.acquire_lease(slug, owner)
        reserved, duplicate = self._reserve(slug, normalized_event, lease)
        if duplicate and reserved.state == "committed":
            self._release(lease)
            return self._receipt(reserved, duplicate=True)
        try:
            receipt = self._project_reserved(reserved, lease)
        except ProjectPendingPriorError:
            self._release(lease)
            raise
        except ProjectJournalRebuildRequired:
            self._set_checkpoint_state(slug, reserved.sequence, "quarantined")
            self._release(lease)
            raise
        except TransactionFailure as exc:
            if exc.code == "precondition_failed":
                self._set_checkpoint_state(slug, reserved.sequence, "quarantined")
                raise ProjectFenceError("project lease changed before checkpoint apply") from exc
            raise
        except BaseException:
            # A prepared transaction remains recoverable while its short lease is valid.
            raise
        else:
            self._release(lease)
            return receipt

    def recover(self, slug: str | None = None) -> list[CheckpointReceipt]:
        if slug is not None:
            _require_slug(slug)
        with self.coordinator._connect() as database:
            query = "SELECT project, sequence FROM project_checkpoints WHERE state != 'committed'"
            parameters: tuple[object, ...] = ()
            if slug is not None:
                query += " AND project = ?"
                parameters = (slug,)
            candidates = {(row["project"], row["sequence"]) for row in database.execute(query, parameters)}

        records = {record.id: record for record in self.coordinator.recover()}
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
                recovered.append(self._project_reserved(row, lease))
            except ProjectPendingPriorError:
                self._release(lease)
                continue
            except TransactionFailure as exc:
                if exc.code == "precondition_failed":
                    self._set_checkpoint_state(
                        project, row.sequence, "quarantined"
                    )
                    continue
                raise
            else:
                self._release(lease)
        return recovered

    def read_journal(self, slug: str) -> str:
        content = self._read_journal_bytes(slug)
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("project journal must be UTF-8") from exc

    def _read_journal_bytes(self, slug: str) -> bytes:
        slug = _require_slug(slug)
        path = self.vault / "knowledge" / "projects" / slug / "journal.md"
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return b""

    def render_state(self, events: Sequence[Mapping[str, object]]) -> bytes:
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
        last_sequence = 0
        for event in ordered:
            validate_schema(event, _SCHEMA)
            project = str(event["project"])
            sequence = int(event["sequence"])
            if sequence <= last_sequence:
                raise ValueError("project journal sequences must increase strictly")
            last_sequence = sequence
            delta = event["delta"]
            assert isinstance(delta, Mapping)
            for name in ("goal", "phase", "current_task"):
                operation = delta[name]
                assert isinstance(operation, Mapping)
                self._reduce(active[name], operation)
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
        return ("\n".join(lines).rstrip() + "\n").encode("utf-8")

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
        self, row: ProjectCheckpointReservation, lease: ProjectLease
    ) -> CheckpointReceipt:
        slug = row.project
        sequence = row.sequence
        event = json.loads(row.event_json)
        with self.coordinator._connect() as database:
            self.coordinator._check_project_head(database, slug, sequence)
        current_journal = self._read_journal_bytes(slug)
        records = self._journal_events(slug, current_journal)
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
        state = self.render_state(records)
        journal_path = f"knowledge/projects/{slug}/journal.md"
        state_path = f"knowledge/projects/{slug}/state.md"
        current_state_path = self.vault / state_path
        changes = [
            MarkdownChange.replace(journal_path, journal)
            if current_journal
            else MarkdownChange.create(journal_path, journal),
            MarkdownChange.replace(state_path, state)
            if current_state_path.exists()
            else MarkdownChange.create(state_path, state),
        ]
        preconditions: dict[str, object] = {
            "project_lease": {
                "project": slug,
                "lease_token": lease.token,
                "fencing_epoch": lease.epoch,
                "expires_at": _timestamp(lease.expires_at),
            },
            journal_path: sha256_bytes(current_journal) if current_journal else ABSENT,
            state_path: sha256_bytes(current_state_path.read_bytes())
            if current_state_path.exists()
            else ABSENT,
        }
        transaction = self.coordinator.prepare(
            changes,
            operation_id=row.operation_id,
            preconditions=preconditions,
            project_reservation=row,
        )
        committed = self.coordinator.apply(transaction.id)
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
        events: list[dict[str, object]] = []
        previous = 0
        for line in text.removeprefix(JOURNAL_HEADER).splitlines():
            if not line:
                continue
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
