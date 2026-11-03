"""Exact protected compile journal schema; operates on caller-owned connections."""
from __future__ import annotations

from llm_wiki.compile_event_records import ProgressCorrupt

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
