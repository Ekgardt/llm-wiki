"""Private, disposable retrieval telemetry stored in rollback-journal SQLite."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memory_state import STATE_ROOT
from reliable_memory import (
    begin_immediate,
    open_operational_db,
    open_readonly_operational_db,
    validate_runtime_file,
)

SCHEMA_VERSION = 1
EVENT_KINDS = frozenset({
    "impression",
    "page_read",
    "evidence_read",
    "context_injected",
    "user_selected",
    "task_outcome",
})
TELEMETRY_DB = STATE_ROOT / "cache" / "evidence-graph" / "telemetry.sqlite3"
DEFAULT_RETENTION_DAYS = 90
DEFAULT_MAX_ROWS = 100_000
DEFAULT_MAX_DELETE = 1_000
MAX_READ_EVENTS = 1_000
MAX_CANDIDATE_IDS = 1_000
MAX_CANDIDATE_SCAN_EVENTS = 100_000
MAX_DATABASE_BYTES = 512 * 1024 * 1024
STRICT_BUSY_MS = 5_000
BEST_EFFORT_BUSY_MS = 25
EXPORT_CURSOR_KEY = "access_export_cursor"

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_EVENT_ID_RE = re.compile(r"[0-9a-f]{32}")
_MAX_LENGTHS = {
    "retrieval_mode": 64,
    "candidate_id": 512,
    "generation": 128,
    "source_tool": 128,
    "outcome": 256,
}


def _is_unsafe_text(name: str, value: str) -> bool:
    return not value or len(value) > _MAX_LENGTHS[name] or any(c in value for c in "\x00\r\n")


def _require_encodable(name: str, value: str) -> None:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} is not valid Unicode") from exc


def _bounded_text(name: str, value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if _is_unsafe_text(name, value):
        raise ValueError(f"{name} is empty, unsafe, or too long")
    _require_encodable(name, value)
    return value


def _parsed_timestamp_text(value: str) -> datetime:
    if any(c in value for c in "\x00\r\n") or len(value) > 40:
        raise ValueError("timestamp is unsafe or too long")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be ISO-8601") from exc


def _parsed_timestamp(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return _parsed_timestamp_text(value)
    raise TypeError("timestamp must be a datetime or string")


def _utc_timestamp(value: datetime | str | None) -> str:
    parsed = _parsed_timestamp(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def hash_query(query: str) -> str:
    """Return the SHA-256 of a UTF-8 query without retaining the query."""
    if not isinstance(query, str):
        raise TypeError("query must be a string")
    try:
        encoded = query.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("query is not valid Unicode") from exc
    return hashlib.sha256(encoded).hexdigest()


def _require_schema_version(value: object) -> None:
    if value != SCHEMA_VERSION or isinstance(value, bool):
        raise ValueError("unsupported telemetry schema version")


def _require_event_id(value: object) -> None:
    if not isinstance(value, str) or _EVENT_ID_RE.fullmatch(value) is None:
        raise ValueError("event_id must be 32 lowercase hexadecimal characters")


def _require_identity(event: object) -> None:
    _require_schema_version(event.schema_version)
    _require_event_id(event.event_id)
    if event.event_kind not in EVENT_KINDS:
        raise ValueError("unknown telemetry event kind")


def _require_query_digest(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError("query_sha256 must be 64 lowercase hexadecimal characters")


def _require_rank(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("rank must be a positive integer")


@dataclass(frozen=True)
class RetrievalEvent:
    schema_version: int
    event_id: str
    event_kind: str
    query_sha256: str | None
    retrieval_mode: str
    candidate_id: str
    rank: int | None
    generation: str
    source_tool: str
    timestamp: str
    outcome: str | None = None

    def __post_init__(self) -> None:
        _require_identity(self)
        _require_query_digest(self.query_sha256)
        _require_rank(self.rank)
        _bounded_text("retrieval_mode", self.retrieval_mode)
        _bounded_text("candidate_id", self.candidate_id)
        _bounded_text("generation", self.generation)
        _bounded_text("source_tool", self.source_tool)
        _bounded_text("outcome", self.outcome, nullable=True)
        if self.timestamp != _utc_timestamp(self.timestamp):
            raise ValueError("timestamp must be canonical UTC")


@dataclass(frozen=True)
class SequencedRetrievalEvent:
    sequence: int
    event: RetrievalEvent

    def __post_init__(self) -> None:
        if (
            not isinstance(self.sequence, int)
            or isinstance(self.sequence, bool)
            or self.sequence <= 0
        ):
            raise ValueError("sequence must be a positive integer")
        if not isinstance(self.event, RetrievalEvent):
            raise TypeError("event must be a RetrievalEvent")


def make_event(
    *,
    event_kind: str,
    retrieval_mode: str,
    candidate_id: str,
    rank: int | None,
    generation: str,
    source_tool: str,
    query: str | None = None,
    query_sha256: str | None = None,
    timestamp: datetime | str | None = None,
    outcome: str | None = None,
) -> RetrievalEvent:
    """Validate and create an event; a raw query is only used transiently."""
    if query is not None and query_sha256 is not None:
        raise ValueError("provide query or query_sha256, not both")
    digest = hash_query(query) if query is not None else query_sha256
    return RetrievalEvent(
        schema_version=SCHEMA_VERSION,
        event_id=uuid.uuid4().hex,
        event_kind=event_kind,
        query_sha256=digest,
        retrieval_mode=retrieval_mode,
        candidate_id=candidate_id,
        rank=rank,
        generation=generation,
        source_tool=source_tool,
        timestamp=_utc_timestamp(timestamp),
        outcome=outcome,
    )


def best_effort_make_event(**values) -> RetrievalEvent | None:
    try:
        return make_event(**values)
    except (TypeError, ValueError, UnicodeError):
        return None


def _ensure_schema(database: sqlite3.Connection) -> None:
    database.execute(
        """
        CREATE TABLE IF NOT EXISTS retrieval_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            schema_version INTEGER NOT NULL,
            event_id TEXT NOT NULL UNIQUE,
            event_kind TEXT NOT NULL,
            query_sha256 TEXT,
            retrieval_mode TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            rank INTEGER,
            generation TEXT NOT NULL,
            source_tool TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            outcome TEXT
        )
        """
    )
    database.execute(
        "CREATE INDEX IF NOT EXISTS retrieval_events_candidate ON retrieval_events(candidate_id, sequence)"
    )
    database.execute(
        "CREATE INDEX IF NOT EXISTS retrieval_events_timestamp ON retrieval_events(timestamp, sequence)"
    )
    database.execute(
        "CREATE INDEX IF NOT EXISTS retrieval_events_kind ON retrieval_events(event_kind, sequence)"
    )
    database.execute(
        """
        CREATE TABLE IF NOT EXISTS telemetry_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )


def _validate_nonnegative_integer(name: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _validate_limit(name: str, value: object, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= maximum
    ):
        raise ValueError(f"{name} is outside the supported range")
    return value


def _validate_export_cursor(value: object) -> str:
    if value == "":
        return ""
    try:
        cursor = _bounded_text("candidate_id", value)
    except (TypeError, ValueError) as exc:
        raise ValueError("export cursor is invalid") from exc
    assert cursor is not None
    return cursor


def _open_write_database(path: Path, *, busy_ms: int) -> sqlite3.Connection:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if metadata.st_size > MAX_DATABASE_BYTES:
            raise ValueError(
                "telemetry cache exceeds the byte ceiling; delete it and regenerate "
                "the disposable cache"
            )
        validate_runtime_file(path, path.parent, max_bytes=MAX_DATABASE_BYTES)
    return open_operational_db(path, busy_ms=busy_ms)


def _is_size_metadata(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_database_size(database: sqlite3.Connection) -> None:
    page_count = database.execute("PRAGMA main.page_count").fetchone()[0]
    page_size = database.execute("PRAGMA main.page_size").fetchone()[0]
    if not _is_size_metadata(page_count) or not _is_size_metadata(page_size):
        raise ValueError("telemetry database size metadata is invalid")
    if page_count * page_size > MAX_DATABASE_BYTES:
        raise ValueError("telemetry database size exceeds the byte ceiling")


def record_events(
    events: list[RetrievalEvent] | tuple[RetrievalEvent, ...],
    *,
    db_path: Path | None = None,
    busy_ms: int = STRICT_BUSY_MS,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> int:
    """Atomically store one bounded batch using one database connection."""
    _require_recordable_batch(events, max_rows)
    if not events:
        return 0
    database = _open_write_database(Path(db_path or TELEMETRY_DB), busy_ms=busy_ms)
    try:
        _ensure_schema(database)
        with begin_immediate(database):
            _trim_for_batch(database, len(events), max_rows)
            _insert_events(database, events)
            _validate_database_size(database)
        return len(events)
    finally:
        database.close()


def _require_batch_shape(events: object, max_rows: int) -> None:
    if not isinstance(events, (list, tuple)):
        raise TypeError("events must be a list or tuple")
    if len(events) > MAX_READ_EVENTS:
        raise ValueError("event batch exceeds limit")
    _validate_limit("max_rows", max_rows, DEFAULT_MAX_ROWS)
    if len(events) > max_rows:
        raise ValueError("event batch exceeds max_rows")


def _require_each_event(events: Sequence[object]) -> None:
    for event in events:
        if not isinstance(event, RetrievalEvent):
            raise TypeError("event batch contains an invalid value")
        event.__post_init__()


def _require_recordable_batch(events: object, max_rows: int) -> None:
    _require_batch_shape(events, max_rows)
    _require_each_event(events)


def _trim_for_batch(database: sqlite3.Connection, arriving: int, max_rows: int) -> None:
    current_rows = database.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM retrieval_events ORDER BY sequence LIMIT ?)",
        (max_rows + MAX_READ_EVENTS + 1,),
    ).fetchone()[0]
    required_delete = max(0, current_rows + arriving - max_rows)
    if required_delete > MAX_READ_EVENTS:
        raise ValueError("telemetry row ceiling requires more than one bounded repair")
    if not required_delete:
        return
    database.execute(
        "DELETE FROM retrieval_events WHERE sequence IN "
        "(SELECT sequence FROM retrieval_events ORDER BY sequence LIMIT ?)",
        (required_delete,),
    )


def _event_row(event: RetrievalEvent) -> tuple:
    return (
        event.schema_version, event.event_id, event.event_kind,
        event.query_sha256, event.retrieval_mode, event.candidate_id,
        event.rank, event.generation, event.source_tool,
        event.timestamp, event.outcome,
    )


def _insert_events(database: sqlite3.Connection, events: Sequence[RetrievalEvent]) -> None:
    database.executemany(
        """
        INSERT INTO retrieval_events (
            schema_version, event_id, event_kind, query_sha256,
            retrieval_mode, candidate_id, rank, generation,
            source_tool, timestamp, outcome
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [_event_row(event) for event in events],
    )


def record_event(
    event: RetrievalEvent,
    *,
    db_path: Path | None = None,
    busy_ms: int = STRICT_BUSY_MS,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> int:
    return record_events(
        [event], db_path=db_path, busy_ms=busy_ms, max_rows=max_rows
    )


def best_effort_record_events(
    events: list[RetrievalEvent] | tuple[RetrievalEvent, ...],
    *,
    db_path: Path | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> bool:
    try:
        record_events(
            events,
            db_path=db_path,
            busy_ms=BEST_EFFORT_BUSY_MS,
            max_rows=max_rows,
        )
        return True
    except Exception:
        return False


def best_effort_record_event(
    event: RetrievalEvent,
    *,
    db_path: Path | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> bool:
    return best_effort_record_events([event], db_path=db_path, max_rows=max_rows)


def _readonly_database(path: Path) -> sqlite3.Connection | None:
    if not path.exists() and not path.is_symlink():
        return None
    return open_readonly_operational_db(
        path,
        path.parent,
        max_bytes=MAX_DATABASE_BYTES,
        owner_only=False,
        busy_ms=STRICT_BUSY_MS,
    )


def _event_from_row(row: sqlite3.Row) -> RetrievalEvent:
    return RetrievalEvent(**{
        key: row[key]
        for key in (
            "schema_version", "event_id", "event_kind", "query_sha256",
            "retrieval_mode", "candidate_id", "rank", "generation",
            "source_tool", "timestamp", "outcome",
        )
    })


def read_events(
    *,
    candidate_id: str | None = None,
    event_kind: str | None = None,
    limit: int = 100,
    db_path: Path | None = None,
) -> list[RetrievalEvent]:
    """Read a hard-bounded newest-first event slice without creating a database."""
    _require_read_filters(candidate_id, event_kind, limit)
    database = _readonly_database(Path(db_path or TELEMETRY_DB))
    if database is None:
        return []
    try:
        where, parameters = _read_filter_clause(candidate_id, event_kind)
        rows = database.execute(
            "SELECT schema_version, event_id, event_kind, query_sha256, retrieval_mode, "
            "candidate_id, rank, generation, source_tool, timestamp, outcome "
            f"FROM retrieval_events{where} ORDER BY sequence DESC LIMIT ?",
            (*parameters, limit),
        ).fetchall()
        return [_event_from_row(row) for row in rows]
    finally:
        database.close()


def _require_read_filters(
    candidate_id: str | None, event_kind: str | None, limit: int
) -> None:
    _validate_limit("limit", limit, MAX_READ_EVENTS)
    if candidate_id is not None:
        _bounded_text("candidate_id", candidate_id)
    if event_kind is not None and event_kind not in EVENT_KINDS:
        raise ValueError("unknown telemetry event kind")


def _present_filters(
    candidate_id: str | None, event_kind: str | None
) -> list[tuple[str, object]]:
    named = (("candidate_id", candidate_id), ("event_kind", event_kind))
    return [(column, value) for column, value in named if value is not None]


def _read_filter_clause(
    candidate_id: str | None, event_kind: str | None
) -> tuple[str, list[object]]:
    present = _present_filters(candidate_id, event_kind)
    if not present:
        return "", []
    where = " WHERE " + " AND ".join(f"{column} = ?" for column, _ in present)
    return where, [value for _, value in present]


def list_candidate_ids(
    *,
    after_candidate: str = "",
    limit: int = 100,
    db_path: Path | None = None,
) -> list[str]:
    """List a bounded lexical candidate page after an exporter cursor."""
    cursor = _validate_export_cursor(after_candidate)
    _validate_limit("limit", limit, MAX_CANDIDATE_IDS)
    path = Path(db_path or TELEMETRY_DB)
    database = _readonly_database(path)
    if database is None:
        return []
    try:
        rows = database.execute(
            "SELECT candidate_id FROM ("
            "SELECT sequence, candidate_id FROM retrieval_events "
            "WHERE candidate_id > ? ORDER BY candidate_id, sequence LIMIT ?"
            ") GROUP BY candidate_id ORDER BY candidate_id LIMIT ?",
            (cursor, MAX_CANDIDATE_SCAN_EVENTS, limit),
        ).fetchall()
        return [str(row[0]) for row in rows]
    finally:
        database.close()


def get_export_cursor(*, db_path: Path | None = None) -> str:
    """Read the durable exporter cursor; an absent database means start."""
    database = _readonly_database(Path(db_path or TELEMETRY_DB))
    if database is None:
        return ""
    try:
        row = _export_cursor_row(database)
        return "" if row is None else _validate_export_cursor(row[0])
    finally:
        database.close()


def _export_cursor_row(database: sqlite3.Connection) -> tuple | None:
    try:
        return database.execute(
            "SELECT value FROM telemetry_state WHERE key = ?",
            (EXPORT_CURSOR_KEY,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise


def set_export_cursor(value: str, *, db_path: Path | None = None) -> None:
    """Atomically persist a validated exporter cursor in telemetry metadata."""
    cursor = _validate_export_cursor(value)
    path = Path(db_path or TELEMETRY_DB)
    database = _open_write_database(path, busy_ms=STRICT_BUSY_MS)
    try:
        _ensure_schema(database)
        with begin_immediate(database):
            database.execute(
                "INSERT INTO telemetry_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (EXPORT_CURSOR_KEY, cursor),
            )
            _validate_database_size(database)
    finally:
        database.close()


def read_events_after(
    candidate_id: str,
    *,
    after_sequence: int,
    limit: int = MAX_READ_EVENTS,
    db_path: Path | None = None,
) -> list[SequencedRetrievalEvent]:
    """Read a bounded oldest-first candidate slice after a sequence."""
    _bounded_text("candidate_id", candidate_id)
    _validate_nonnegative_integer("after_sequence", after_sequence)
    _validate_limit("limit", limit, MAX_READ_EVENTS)
    path = Path(db_path or TELEMETRY_DB)
    database = _readonly_database(path)
    if database is None:
        return []
    try:
        rows = database.execute(
            "SELECT sequence, schema_version, event_id, event_kind, query_sha256, "
            "retrieval_mode, candidate_id, rank, generation, source_tool, timestamp, outcome "
            "FROM retrieval_events WHERE candidate_id = ? AND sequence > ? "
            "ORDER BY sequence LIMIT ?",
            (candidate_id, after_sequence, limit),
        ).fetchall()
        return [
            SequencedRetrievalEvent(sequence=row["sequence"], event=_event_from_row(row))
            for row in rows
        ]
    finally:
        database.close()


def count_events_after(
    candidate_id: str,
    *,
    after_sequence: int,
    limit: int = MAX_READ_EVENTS,
    db_path: Path | None = None,
) -> int:
    """Count at most ``limit`` candidate events after a sequence."""
    _bounded_text("candidate_id", candidate_id)
    _validate_nonnegative_integer("after_sequence", after_sequence)
    _validate_limit("limit", limit, MAX_READ_EVENTS)
    path = Path(db_path or TELEMETRY_DB)
    database = _readonly_database(path)
    if database is None:
        return 0
    try:
        return int(database.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM retrieval_events "
            "WHERE candidate_id = ? AND sequence > ? ORDER BY sequence LIMIT ?)",
            (candidate_id, after_sequence, limit),
        ).fetchone()[0])
    finally:
        database.close()


def compact(
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_delete: int = DEFAULT_MAX_DELETE,
    now: datetime | None = None,
    db_path: Path | None = None,
) -> int:
    """Delete an oldest-first bounded slice of expired or excess telemetry."""
    _require_compact_limits(retention_days, max_rows, max_delete)
    path = Path(db_path or TELEMETRY_DB)
    if not path.exists():
        return 0
    cutoff = _utc_timestamp(_compact_cutoff(now, retention_days))
    database = _open_write_database(path, busy_ms=STRICT_BUSY_MS)
    try:
        _ensure_schema(database)
        with begin_immediate(database):
            delete_count = _expired_delete_count(database, cutoff, max_rows, max_delete)
            _delete_oldest(database, delete_count)
        return delete_count
    finally:
        database.close()


def _require_compact_limits(retention_days: int, max_rows: int, max_delete: int) -> None:
    named = (
        ("retention_days", retention_days, 0),
        ("max_rows", max_rows, 0),
        ("max_delete", max_delete, 1),
    )
    for name, value, minimum in named:
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ValueError(f"{name} is invalid")


def _compact_cutoff(now: datetime | None, retention_days: int) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return current - timedelta(days=retention_days)


def _expired_delete_count(
    database: sqlite3.Connection, cutoff: str, max_rows: int, max_delete: int
) -> int:
    expired = database.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM retrieval_events WHERE timestamp < ? LIMIT ?)",
        (cutoff, max_delete),
    ).fetchone()[0]
    total = database.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM retrieval_events LIMIT ?)",
        (max_rows + max_delete,),
    ).fetchone()[0]
    return min(max_delete, max(expired, total - max_rows))


def _delete_oldest(database: sqlite3.Connection, delete_count: int) -> None:
    if not delete_count:
        return
    database.execute(
        "DELETE FROM retrieval_events WHERE sequence IN "
        "(SELECT sequence FROM retrieval_events ORDER BY timestamp, sequence LIMIT ?)",
        (delete_count,),
    )
