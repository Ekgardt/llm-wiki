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

from reliable_memory import operational_deadline_active

QUEUE_SCHEMA_VERSION = 4

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


def event_at(database: sqlite3.Connection, work_id: str, sequence: int) -> CompileEvent | None:
    """Read one verified historical event for deterministic replay, without a scan."""
    _require_digest(work_id)
    _require_sequence(sequence)
    row = database.execute(
        'SELECT * FROM compile_work_events WHERE work_id=? AND sequence=?',
        (work_id, sequence),
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


def _require_schema_object(database, kind: str, name: str, sql: str) -> None:
    row = database.execute(
        'SELECT type,sql FROM sqlite_master WHERE name=?', (name,),
    ).fetchone()
    if row is None or (row['type'], row['sql']) != (kind, sql.strip()):
        raise ProgressCorrupt('compile journal schema or protection changed')


def _require_schema(database) -> None:
    objects = (
        ('table', 'compile_work_events'),
        ('trigger', 'compile_work_events_no_update'),
        ('trigger', 'compile_work_events_no_delete'),
    )
    for (kind, name), sql in zip(objects, SCHEMA_STATEMENTS):
        _require_schema_object(database, kind, name, sql)


def _audit_event(row, previous, counts, verify_artifact):
    event = _verified_event(row)
    if previous is None or previous.work_id != event.work_id:
        previous = None
        counts['works'] += 1
    _require_next(previous, event)
    if 'artifact' in event.details:
        artifact_identity(event)
        verify_artifact(event)
        counts['artifacts'] += 1
    counts['events'] += 1
    return event


def audit_events(database, verify_artifact, *, deadline=None) -> dict[str, int]:
    """Stream retained history; certify neither publication nor a deletion permit."""
    operational_deadline_active(deadline)
    counts = {'events': 0, 'works': 0, 'artifacts': 0}
    if not _journal_present(database):
        return counts
    _require_schema(database)
    previous = None
    for row in database.execute('SELECT * FROM compile_work_events ORDER BY work_id,sequence'):
        operational_deadline_active(deadline)
        previous = _audit_event(row, previous, counts, verify_artifact)
    operational_deadline_active(deadline)
    return counts


def retains_artifact(database: sqlite3.Connection, reference: str) -> bool:
    """Protect every retained event's file, including superseded checkpoints.

    No journal is normal for v3. A v4 opener independently requires its schema.
    Malformed evidence refuses deletion rather than guessing that a file is free.
    The caller must hold the queue write transaction through the subsequent unlink.
    """
    if not _journal_present(database):
        return False
    _require_schema(database)
    return any(_references_artifact(row, reference) for row in database.execute(
        'SELECT * FROM compile_work_events ORDER BY work_id,sequence',
    ))


def _references_artifact(row: sqlite3.Row, reference: str) -> bool:
    event = _verified_event(row)
    if 'artifact' not in event.details:
        return False
    return artifact_identity(event)[0] == reference


def _journal_present(database: sqlite3.Connection) -> bool:
    return database.execute(
        "SELECT 1 FROM sqlite_master WHERE name='compile_work_events'",
    ).fetchone() is not None


def retains_task(database: sqlite3.Connection, task_id: str) -> bool:
    """A task named by retained history is not eligible for ordinary purge."""
    if not _journal_present(database):
        return False
    _require_schema(database)
    return database.execute(
        'SELECT 1 FROM compile_work_events WHERE task_id=? LIMIT 1', (task_id,),
    ).fetchone() is not None


def root_work_id(origin_task_id: str) -> str:
    """Stable lookup address; immutable event zero binds the semantic identity."""
    if not isinstance(origin_task_id, str) or not origin_task_id:
        raise ValueError('compile origin task identity must be nonempty text')
    value = {'schema': 'compile-root/v1', 'origin_task_id': origin_task_id}
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def work_identity(origin, source_digest, dependency_digest, request_digest):
    root_work_id(origin)
    for digest in (source_digest, dependency_digest, request_digest):
        _require_digest(digest)
    value = {'schema': 'compile-work/v1', 'origin_task_id': origin,
             'source_digest': source_digest, 'dependency_digest': dependency_digest,
             'request_digest': request_digest}
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def root_details(origin, source_digest, dependency_digest, request_digest):
    identity = work_identity(origin, source_digest, dependency_digest, request_digest)
    return {'schema': 'compile-work/v1', 'origin_task_id': origin,
            'dependency_digest': dependency_digest, 'request_digest': request_digest,
            'identity': identity}


def _require_root_details_shape(details):
    fields = {'schema', 'origin_task_id', 'dependency_digest', 'request_digest', 'identity'}
    if not isinstance(details, dict) or set(details) != fields:
        raise ProgressCorrupt('compile root identity is missing or changed')


def validate_root_event(event, origin):
    if event.work_id != root_work_id(origin):
        raise ProgressConflict('compile root belongs to another origin')
    details = event.details.get('compile_work')
    _require_root_details_shape(details)
    expected = root_details(origin, event.source_digest,
                            details['dependency_digest'], details['request_digest'])
    if details != expected:
        raise ProgressCorrupt('compile root semantic identity changed')


def completed_work(database, origin):
    """Read the compiler's immutable whole-work attestation, not live receipts."""
    root = event_at(database, root_work_id(origin), 0)
    if root is None:
        return None
    _require_initial(root)
    validate_root_event(root, origin)
    latest = latest_event(database, root.work_id)
    if (latest.stage, latest.outcome) != ('published', 'completed'):
        return None
    proof = _work_completion_proof(latest)
    parent = event_at(database, root.work_id, latest.sequence - 1)
    _require_completion_preflight(database, root, parent, proof)
    return latest


def _work_completion_proof(event):
    if set(event.details) != {'work_completion'}:
        raise ProgressCorrupt('compile root has no whole-work completion proof')
    proof = event.details['work_completion']
    fields = {'schema', 'preflight_digest', 'batch_count', 'children_digest'}
    if not isinstance(proof, dict) or set(proof) != fields:
        raise ProgressCorrupt('invalid whole-work completion fields')
    _require_work_completion_values(proof)
    return proof


def _require_work_completion_values(proof):
    if proof['schema'] != 'compile-work-completion/v1':
        raise ProgressCorrupt('unsupported whole-work completion schema')
    _require_sequence(proof['batch_count'])
    for name in ('preflight_digest', 'children_digest'):
        _require_digest(proof[name])


def _require_completion_preflight(database, root, parent, proof):
    if parent is None or (parent.sequence, parent.stage, parent.outcome) != (2, 'pending', 'ready'):
        raise ProgressCorrupt('whole-work completion has no retained preflight')
    if _event_digest(parent) != proof['preflight_digest'] or parent.source_digest != root.source_digest:
        raise ProgressCorrupt('whole-work completion preflight identity changed')
    snapshot = event_at(database, root.work_id, 1)
    _require_completion_snapshot(snapshot, root)
    expected = {'schema': 'compile-preflight/v1', 'snapshot_digest': _event_digest(snapshot),
                'dependency_digest': root.details['compile_work']['dependency_digest'],
                'request_digest': root.details['compile_work']['request_digest']}
    _require_completion_preflight_binding(parent, expected)


def _require_completion_snapshot(snapshot, root):
    if snapshot is None:
        raise ProgressCorrupt('whole-work completion has no retained snapshot')
    if (snapshot.stage, snapshot.outcome, snapshot.source_digest) != ('snapshot_ready', 'ready', root.source_digest):
        raise ProgressCorrupt('whole-work completion snapshot identity changed')
    artifact_identity(snapshot)


def _require_completion_preflight_binding(parent, expected):
    if set(parent.details) != {'compile_preflight', 'artifact'} or parent.details['compile_preflight'] != expected:
        raise ProgressCorrupt('whole-work completion binding changed')
    artifact_identity(parent)
