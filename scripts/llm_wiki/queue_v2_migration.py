"""Building a version 3 queue database from a version 2 one."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import ExitStack, closing
from pathlib import Path
from types import MappingProxyType

from llm_wiki import queue_store
from llm_wiki.queue_artifacts import harden_owner_only
from llm_wiki.queue_database_contract import QUEUE_V3_CONTRACT
from llm_wiki.queue_migration_plan import (
    queue_v2_migration_statements,
    queue_v2_reconciliation_complete,
)
from llm_wiki.queue_schema import QUEUE_V3_TABLE_SQL
from llm_wiki.queue_schema_operations import (
    queue_v3_schema_complete,
    queue_v3_statements,
    queue_v3_trigger_statements,
    repair_partial_queue_v3_schema,
)
from llm_wiki.queue_timing import timestamp
from llm_wiki.queue_v3_candidate import queue_v3_invariants_hold, validate_queue_v3_candidate
from llm_wiki.queue_v3_readers import audit_compile_progress, open_queue_v3_readonly
from llm_wiki.reliability_defaults import DEFAULTS
from llm_wiki.sqlite_contracts import MigrationStatement, migration_error
from llm_wiki.sqlite_migrations import run_resumable_migration
from reliable_memory import open_operational_db, open_readonly_operational_db


def _still_held(source: sqlite3.Connection, table: str, where: str) -> bool:
    """Whether any such row is inside its lease; a null expiry cannot say, so it is.

    A fence row outlives the process that took it — the live v2 queue sweeps
    those on every open — and refusing adoption for a leftover locks a vault out
    for good. See `docs/research/
    2026-09-18-an-expired-fence-is-not-an-obstacle-to-adoption.md`.
    """
    now = timestamp(queue_store.utc_now())
    rows = source.execute(
        f"SELECT expires_at FROM {table} WHERE {where}"  # noqa: S608 - fixed names
    ).fetchall()
    return any(row["expires_at"] is None or str(row["expires_at"]) > now for row in rows)


def _require_unambiguous_v2_owners(
    source: sqlite3.Connection, tables: set[str]
) -> None:
    """A v2 fence still held predates the identity v3 needs to fence with."""
    if "source_fences" in tables and _still_held(source, "source_fences", "1=1"):
        raise migration_error(
            "queue_v2_source_fence_ambiguous",
            "queue v2 source fences lack process-start identity",
        )
    if "queue_ownership" in tables and _still_held(
        source, "queue_ownership", "token IS NOT NULL"
    ):
        raise migration_error(
            "queue_v2_owner_ambiguous",
            "queue v2 ownership lacks canonical process identity",
        )


def _queue_v3_row_counts(database: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(database.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table, _sql in QUEUE_V3_TABLE_SQL
    }


def _names_the_same_file(candidate: Path, source: Path) -> bool:
    """Whether an existing candidate resolves to the very same file."""
    if not candidate.exists() and not candidate.is_symlink():
        return False
    return os.path.samefile(candidate, source)


def _reject_queue_source_alias(candidate: Path, source: Path) -> None:
    if candidate.absolute() == source.absolute():
        raise ValueError("queue v3 candidate and v2 source are the same file")
    try:
        aliased = _names_the_same_file(candidate, source)
    except OSError as exc:
        raise PermissionError(
            "queue v3 candidate identity could not be verified"
        ) from exc
    if aliased:
        raise ValueError("queue v3 candidate and v2 source are the same file")


_EMPTY_QUEUE_V3_SUMMARY = MappingProxyType(
    {
        "attempt_history": 0,
        "payload_hash_mismatches": 0,
        "source_failures": 0,
        "source_fences": 0,
        "task_source_links": 0,
        "tasks": 0,
    }
)


def _open_queue_v2_source(
    stack: ExitStack, candidate: Path, source_path: Path
) -> sqlite3.Connection:
    """Open the v2 source read-only, refusing one that aliases the candidate."""
    _reject_queue_source_alias(candidate, source_path)
    source = stack.enter_context(
        closing(
            open_readonly_operational_db(
                source_path,
                source_path.parent.parent,
                max_bytes=1 << 50,
                owner_only=True,
            )
        )
    )
    source.row_factory = sqlite3.Row
    source.execute("BEGIN")
    return source


def _queue_v2_migration_plan(
    stack: ExitStack, candidate: Path, source_path: Path | None
) -> tuple[tuple[MigrationStatement, ...], dict[str, int]]:
    """What the v2 source contributes, or nothing when there is no source."""
    if source_path is None:
        return (), dict(_EMPTY_QUEUE_V3_SUMMARY)
    source = _open_queue_v2_source(stack, candidate, source_path)
    statements, summary = queue_v2_migration_statements(
        source, check_owners=_require_unambiguous_v2_owners
    )
    _reject_queue_source_alias(candidate, source_path)
    return statements, summary


def require_empty_queue_v3(database: sqlite3.Connection) -> None:
    """A fresh initialization may not find rows already there."""
    if any(_queue_v3_row_counts(database).values()):
        raise migration_error(
            "queue_v3_source_conflict",
            "fresh queue v3 initialization found existing rows",
        )


def _build_queue_v3_candidate(
    stack: ExitStack,
    candidate: Path,
    source_path: Path | None,
    killpoint: Callable[[str], None] | None,
) -> dict[str, int]:
    """Create or resume the candidate's schema and content, then validate it."""
    migration_statements, summary = _queue_v2_migration_plan(
        stack, candidate, source_path
    )
    database = stack.enter_context(
        closing(open_operational_db(candidate, busy_ms=DEFAULTS.queue_busy_ms))
    )
    if source_path is not None:
        _reject_queue_source_alias(candidate, source_path)
    repair_partial_queue_v3_schema(
        database, allow_populated_rebuild=source_path is not None
    )
    run_resumable_migration(
        database,
        queue_v3_statements(include_triggers=False),
        final_invariant=lambda current: queue_v3_schema_complete(
            current, include_triggers=False
        ),
        killpoint=killpoint,
    )
    _apply_queue_v2_content(
        database, migration_statements, summary, source_path, killpoint
    )
    run_resumable_migration(
        database,
        queue_v3_trigger_statements(),
        final_invariant=queue_v3_schema_complete,
        killpoint=killpoint,
    )
    validate_queue_v3_candidate(database)
    return summary


def _apply_queue_v2_content(
    database: sqlite3.Connection,
    migration_statements: tuple[MigrationStatement, ...],
    summary: Mapping[str, int],
    source_path: Path | None,
    killpoint: Callable[[str], None] | None,
) -> None:
    """Copy the v2 rows in, or prove the fresh candidate has none."""
    if source_path is None:
        require_empty_queue_v3(database)
        return
    run_resumable_migration(
        database,
        migration_statements,
        final_invariant=lambda current: queue_v2_reconciliation_complete(
            current, migration_statements, summary
        ),
        killpoint=killpoint,
    )


def _stamp_queue_v3_contract(candidate: Path) -> None:
    """Write the contract row that marks the candidate as a v3 database."""
    with closing(
        open_operational_db(
            candidate,
            busy_ms=DEFAULTS.queue_busy_ms,
            contract=QUEUE_V3_CONTRACT,
            initialize_contract=True,
        )
    ):
        pass


def initialize_queue_v3_candidate(
    path: Path,
    *,
    source_v2: Path | None,
    killpoint: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Create or resume an unpublished queue v3 candidate database."""
    candidate = Path(path)
    source_path = Path(source_v2) if source_v2 is not None else None
    with ExitStack() as stack:
        summary = _build_queue_v3_candidate(
            stack, candidate, source_path, killpoint
        )
    _stamp_queue_v3_contract(candidate)
    harden_owner_only(candidate, 0o600)
    validate_queue_v3_database(candidate, state_root=candidate.parent.parent)
    return summary


def _require_inside_state_root(path: Path, state_root: Path) -> None:
    """Refuse a database that does not live under the runtime root."""
    try:
        path.resolve(strict=True).relative_to(state_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise PermissionError("queue v3 database is outside the state root") from exc


def queue_v3_report(database: sqlite3.Connection) -> dict[str, object]:
    """The health record a validated queue v3 database answers with."""
    return {
        "application_id": database.execute("PRAGMA application_id").fetchone()[0],
        "foreign_key_check": [],
        "integrity_check": "ok",
        "journal_mode": database.execute("PRAGMA journal_mode").fetchone()[0],
        "row_counts": _queue_v3_row_counts(database),
        "synchronous": database.execute("PRAGMA synchronous").fetchone()[0],
        "trusted_schema": database.execute("PRAGMA trusted_schema").fetchone()[0],
        "user_version": database.execute("PRAGMA user_version").fetchone()[0],
    }


def require_queue_v3_openable(path: Path, *, state_root: Path, deadline: float | None = None, expected_contract=None) -> None:
    """What every open of the adopted queue checks: place, contract, schema.

    The whole-file check (`validate_queue_v3_database`) reads every retained row,
    so it belongs where a file is certified — adoption, backup, a candidate —
    and not on each hook's open. A bad payload is a fact about one task; the
    transition that touches that task demotes it, which a veto on open prevents.
    """
    _require_inside_state_root(Path(path), Path(state_root))
    with closing(open_queue_v3_readonly(
        Path(path), Path(state_root), deadline=deadline, expected_contract=expected_contract,
    )) as database:
        if not queue_v3_schema_complete(database):
            raise migration_error(
                "queue_v3_schema_incomplete", "queue v3 schema is incomplete"
            )


def validate_queue_v3_database(
    path: Path, *, state_root: Path
) -> dict[str, object]:
    """Validate one unpublished or active queue v3 database fail-closed."""
    _require_inside_state_root(Path(path), Path(state_root))
    with closing(open_queue_v3_readonly(Path(path), Path(state_root))) as database:
        if not queue_v3_schema_complete(database):
            raise migration_error(
                "queue_v3_schema_incomplete", "queue v3 schema is incomplete"
            )
        if not queue_v3_invariants_hold(database):
            raise migration_error(
                "queue_v3_validation_failed", "queue v3 database invariant failed"
            )
        report = queue_v3_report(database)
        report["compile_progress"] = audit_compile_progress(database, state_root)
        return report
