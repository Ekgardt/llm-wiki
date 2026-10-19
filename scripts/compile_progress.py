"""Append-only compile checkpoint storage inside the existing queue database.

This is the storage boundary, not a workflow validator or a second owner registry.
Callers must verify the queue lease, payload and semantic transition inside their
BEGIN IMMEDIATE transaction before appending. Schema statements belong to the
explicit offline migration; reading or appending never initializes a database.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

SCHEMA_STATEMENTS = (
    """CREATE TABLE compile_work_events (
        work_id TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK(sequence >= 0),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        source_digest TEXT NOT NULL,
        stage TEXT NOT NULL,
        outcome TEXT NOT NULL,
        details_json TEXT NOT NULL,
        event_digest TEXT NOT NULL,
        PRIMARY KEY(work_id, sequence)
    )""",
    """CREATE TRIGGER compile_work_events_no_update
       BEFORE UPDATE ON compile_work_events
       BEGIN SELECT RAISE(ABORT, 'compile events are immutable'); END""",
    """CREATE TRIGGER compile_work_events_no_delete
       BEFORE DELETE ON compile_work_events
       BEGIN SELECT RAISE(ABORT, 'compile event retirement requires migration'); END""",
)
_STAGES = frozenset((
    'pending', 'snapshot_ready', 'draft_ready', 'review_ready',
    'publication_ready', 'published',
))
_OUTCOMES = frozenset(('ready', 'running', 'waiting', 'blocked', 'failed', 'completed'))
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')


class ProgressConflict(ValueError):
    """The caller no longer holds the progress revision it expected."""


class ProgressCorrupt(ValueError):
    """Stored checkpoint bytes or their identity failed validation."""


@dataclass(frozen=True)
class CompileEvent:
    work_id: str
    task_id: str
    sequence: int
    source_digest: str
    stage: str
    outcome: str
    details: dict[str, Any]


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def _event_digest(event: CompileEvent) -> str:
    return hashlib.sha256(_canonical(asdict(event)).encode('utf-8')).hexdigest()


def _require_digest(value: object) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError('compile identity must be a SHA-256 hex digest')


def _require_sequence(value: object) -> None:
    if type(value) is not int or value < 0:
        raise ValueError('compile sequence must be a nonnegative integer')


def _require_labels(event: CompileEvent) -> None:
    if event.stage not in _STAGES:
        raise ValueError('unknown compile stage')
    if event.outcome not in _OUTCOMES:
        raise ValueError('unknown compile outcome')


def _require_details(event: CompileEvent) -> None:
    if not isinstance(event.details, dict):
        raise TypeError('compile event details must be an object')
    if not isinstance(event.task_id, str) or not event.task_id:
        raise ValueError('compile task identity must be nonempty text')
    _canonical(event.details)


def _validate_event(event: CompileEvent) -> None:
    _require_digest(event.work_id)
    _require_digest(event.source_digest)
    _require_sequence(event.sequence)
    _require_labels(event)
    _require_details(event)


def _snapshot_event(event: CompileEvent) -> CompileEvent:
    _validate_event(event)
    snapshot = CompileEvent(**json.loads(_canonical(asdict(event))))
    if snapshot != event:
        raise ValueError('compile event must contain lossless JSON values')
    return snapshot


def artifact_identity(event: CompileEvent) -> tuple[str, str]:
    """Accept only a queue-result filename, never an arbitrary path from metadata."""
    try:
        artifact = event.details['artifact']
        relative, digest = artifact['reference'], artifact['sha256']
        _require_digest(digest)
        safe = re.fullmatch(r'run/queue-results/[0-9a-f]{64}\.result', relative)
    except (KeyError, TypeError, ValueError) as error:
        raise ProgressCorrupt('invalid compile artifact reference') from error
    if safe is None:
        raise ProgressCorrupt('compile artifact path escapes result storage')
    return relative, digest


def _decoded_event(row: sqlite3.Row) -> CompileEvent:
    return CompileEvent(
        work_id=row['work_id'], task_id=row['task_id'], sequence=row['sequence'],
        source_digest=row['source_digest'], stage=row['stage'], outcome=row['outcome'],
        details=json.loads(row['details_json']),
    )


def _verified_event(row: sqlite3.Row) -> CompileEvent:
    try:
        event = _decoded_event(row)
        _validate_event(event)
        intact = _event_digest(event) == row['event_digest']
    except (ValueError, TypeError, KeyError) as error:
        raise ProgressCorrupt('invalid stored compile event') from error
    if not intact:
        raise ProgressCorrupt('compile event digest mismatch')
    return event


def latest_event(database: sqlite3.Connection, work_id: str) -> CompileEvent | None:
    """Read one indexed checkpoint; absence never creates schema or progress."""
    _require_digest(work_id)
    row = database.execute(
        'SELECT * FROM compile_work_events WHERE work_id=? '
        'ORDER BY sequence DESC LIMIT 1', (work_id,),
    ).fetchone()
    if row is None:
        return None
    return _verified_event(row)


def _require_next(previous: CompileEvent | None, event: CompileEvent) -> None:
    if previous is None:
        _require_initial(event)
        return
    _require_successor(previous, event)


def _require_initial(event: CompileEvent) -> None:
    if event.sequence != 0 or event.stage != 'pending':
        raise ProgressConflict('compile work must start at pending sequence zero')


def _require_successor(previous: CompileEvent, event: CompileEvent) -> None:
    if event.sequence != previous.sequence + 1:
        raise ProgressConflict('compile progress revision changed')
    if event.source_digest != previous.source_digest:
        raise ProgressConflict('compile source identity changed')


def append_event(database: sqlite3.Connection, event: CompileEvent) -> None:
    """Append after caller-owned fencing; never acquire or commit its transaction.

    The content digest detects accidental corruption, not a hostile database
    editor. The SQL primary key also rejects two writers using the same revision.
    Retained history prevents task purge through the existing foreign-key boundary.
    """
    if not database.in_transaction:
        raise ProgressConflict('compile append requires the caller transaction')
    event = _snapshot_event(event)
    previous = latest_event(database, event.work_id)
    _require_next(previous, event)
    database.execute(
        'INSERT INTO compile_work_events '
        '(work_id,sequence,task_id,source_digest,stage,outcome,details_json,event_digest) '
        'VALUES (?,?,?,?,?,?,?,?)',
        (event.work_id, event.sequence, event.task_id, event.source_digest,
         event.stage, event.outcome, _canonical(event.details), _event_digest(event)),
    )
