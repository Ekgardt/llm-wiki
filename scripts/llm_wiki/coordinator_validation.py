"""Coordinator file admission and full certification through existing SQLite IO."""
from __future__ import annotations

import contextlib
from pathlib import Path

import llm_wiki.sqlite_io as sqlite_io
from llm_wiki.coordinator_invariants import (
    coordinator_migration_error,
    coordinator_v3_row_counts,
    require_v3_invariants,
)
from llm_wiki.coordinator_schema import COORDINATOR_V3_CONTRACT, coordinator_v3_schema_complete
from llm_wiki.reliability_defaults import DEFAULTS


def require_inside_state_root(path: Path, state_root: Path) -> None:
    try:
        path.resolve(strict=True).relative_to(state_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise PermissionError(
            "coordinator v3 database is outside the state root"
        ) from exc


def require_coordinator_v3_openable(path: Path, *, state_root: Path, deadline: float | None = None) -> None:
    """Check the adopted file's place, connection contract and complete schema."""
    require_inside_state_root(Path(path), Path(state_root))
    with contextlib.closing(sqlite_io.open_readonly_operational_db(Path(path), Path(state_root), max_bytes=1 << 50, owner_only=True, busy_ms=DEFAULTS.markdown_busy_ms, contract=COORDINATOR_V3_CONTRACT, deadline=deadline)) as database:
        if not coordinator_v3_schema_complete(database):
            raise coordinator_migration_error("coordinator_v3_schema_incomplete", "coordinator v3 schema is incomplete")


def reliability_v3_records_present(state_root: Path) -> bool:
    run_root = Path(state_root) / "run"
    records = (
        run_root / "reliability-v3-migration.json",
        run_root / "reliability-v3-adopted.json",
    )
    return any(path.exists() or path.is_symlink() for path in records)



def validate_coordinator_v3_database(
    path: Path, *, state_root: Path
) -> dict[str, object]:
    """Validate one unpublished or active coordinator v3 database fail-closed."""
    require_inside_state_root(Path(path), Path(state_root))
    with contextlib.closing(
        sqlite_io.open_readonly_operational_db(
            Path(path),
            Path(state_root),
            max_bytes=1 << 50,
            owner_only=True,
            busy_ms=DEFAULTS.markdown_busy_ms,
            contract=COORDINATOR_V3_CONTRACT,
        )
    ) as database:
        if not coordinator_v3_schema_complete(database):
            raise coordinator_migration_error(
                "coordinator_v3_schema_incomplete",
                "coordinator v3 schema is incomplete",
            )
        require_v3_invariants(database)
        return {
            "application_id": database.execute("PRAGMA application_id").fetchone()[0],
            "foreign_key_check": [],
            "integrity_check": "ok",
            "journal_mode": database.execute("PRAGMA journal_mode").fetchone()[0],
            "row_counts": coordinator_v3_row_counts(database),
            "synchronous": database.execute("PRAGMA synchronous").fetchone()[0],
            "trusted_schema": database.execute("PRAGMA trusted_schema").fetchone()[0],
            "user_version": database.execute("PRAGMA user_version").fetchone()[0],
        }

# Keep historical callable serialization paths through identical facade aliases.
require_inside_state_root.__module__ = "markdown_transaction"
require_coordinator_v3_openable.__module__ = "markdown_transaction"
validate_coordinator_v3_database.__module__ = "markdown_transaction"
