"""Coordinator file admission and full certification through existing SQLite IO."""
from __future__ import annotations

import contextlib
from pathlib import Path

from llm_wiki.coordinator_invariants import (
    _coordinator_migration_error,
    _coordinator_v3_row_counts,
    _require_v3_invariants,
)
from llm_wiki.coordinator_schema import _COORDINATOR_V3_CONTRACT, _coordinator_v3_schema_complete
from llm_wiki.reliability_defaults import DEFAULTS
from llm_wiki.sqlite_io import open_readonly_operational_db


def _require_inside_state_root(path: Path, state_root: Path) -> None:
    try:
        path.resolve(strict=True).relative_to(state_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise PermissionError(
            "coordinator v3 database is outside the state root"
        ) from exc


def require_coordinator_v3_openable(path: Path, *, state_root: Path, deadline: float | None = None) -> None:
    """Check the adopted file's place, connection contract and complete schema."""
    _require_inside_state_root(Path(path), Path(state_root))
    with contextlib.closing(open_readonly_operational_db(Path(path), Path(state_root), max_bytes=1 << 50, owner_only=True, busy_ms=DEFAULTS.markdown_busy_ms, contract=_COORDINATOR_V3_CONTRACT, deadline=deadline)) as database:
        if not _coordinator_v3_schema_complete(database):
            raise _coordinator_migration_error("coordinator_v3_schema_incomplete", "coordinator v3 schema is incomplete")


def validate_coordinator_v3_database(
    path: Path, *, state_root: Path
) -> dict[str, object]:
    """Validate one unpublished or active coordinator v3 database fail-closed."""
    _require_inside_state_root(Path(path), Path(state_root))
    with contextlib.closing(
        open_readonly_operational_db(
            Path(path),
            Path(state_root),
            max_bytes=1 << 50,
            owner_only=True,
            busy_ms=DEFAULTS.markdown_busy_ms,
            contract=_COORDINATOR_V3_CONTRACT,
        )
    ) as database:
        if not _coordinator_v3_schema_complete(database):
            raise _coordinator_migration_error(
                "coordinator_v3_schema_incomplete",
                "coordinator v3 schema is incomplete",
            )
        _require_v3_invariants(database)
        return {
            "application_id": database.execute("PRAGMA application_id").fetchone()[0],
            "foreign_key_check": [],
            "integrity_check": "ok",
            "journal_mode": database.execute("PRAGMA journal_mode").fetchone()[0],
            "row_counts": _coordinator_v3_row_counts(database),
            "synchronous": database.execute("PRAGMA synchronous").fetchone()[0],
            "trusted_schema": database.execute("PRAGMA trusted_schema").fetchone()[0],
            "user_version": database.execute("PRAGMA user_version").fetchone()[0],
        }

# Keep historical callable serialization paths through identical facade aliases.
_require_inside_state_root.__module__ = "markdown_transaction"
require_coordinator_v3_openable.__module__ = "markdown_transaction"
validate_coordinator_v3_database.__module__ = "markdown_transaction"
