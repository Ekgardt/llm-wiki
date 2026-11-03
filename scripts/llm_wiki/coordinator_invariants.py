"""Read-only coordinator invariants on a caller-owned SQLite connection."""
from __future__ import annotations

import sqlite3

from llm_wiki.coordinator_schema import _COORDINATOR_V3_TABLE_SQL
from llm_wiki.sqlite_contracts import OperationalDatabaseContractError


def _coordinator_migration_error(
    code: str, message: str
) -> OperationalDatabaseContractError:
    error = OperationalDatabaseContractError(message)
    error.code = code
    return error


def _coordinator_v3_row_counts(database: sqlite3.Connection) -> dict[str, int]:
    return {
        name: int(database.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
        for name, _sql in _COORDINATOR_V3_TABLE_SQL
    }


def _coordinator_v3_base_cross_table_invariant(database: sqlite3.Connection) -> bool:
    owner_projection_violations = database.execute(
        """SELECT COUNT(*) FROM (
               SELECT process_id, process_start_identity, canonical_role,
                      canonical_scope, actor_id, owner_token, fencing_epoch
               FROM writer_owners
               UNION ALL
               SELECT process_id, process_start_identity, canonical_role,
                      canonical_scope, actor_id, lease_token, fencing_epoch
               FROM project_leases
               UNION ALL
               SELECT process_id, process_start_identity, canonical_role,
                      canonical_scope, canonical_actor_id, canonical_owner_token,
                      canonical_fencing_epoch
               FROM intent_fences
           ) AS projection
           LEFT JOIN maintenance_owners AS owner
             ON owner.role=projection.canonical_role
            AND owner.scope=projection.canonical_scope
            AND owner.actor_id=projection.actor_id
            AND owner.owner_token=projection.owner_token
            AND owner.fencing_epoch=projection.fencing_epoch
           WHERE owner.role IS NULL
              OR owner.process_id != projection.process_id
              OR owner.process_start_identity != projection.process_start_identity"""
    ).fetchone()[0]
    capture_projection_violations = database.execute(
        """SELECT COUNT(*) FROM capture_binding_projections AS binding
           LEFT JOIN intent_fences AS fence
             ON fence.intent_id=binding.intent_id
            AND fence.token=binding.intent_fence_token
            AND fence.fencing_epoch=binding.intent_fence_epoch
           WHERE fence.intent_id IS NULL"""
    ).fetchone()[0]
    owner_epoch_violations = database.execute(
        """SELECT COUNT(*) FROM maintenance_owners AS owner
           LEFT JOIN maintenance_owner_epochs AS epoch
             ON epoch.role=owner.role AND epoch.scope=owner.scope
           WHERE epoch.role IS NULL OR epoch.last_epoch < owner.fencing_epoch"""
    ).fetchone()[0]
    intent_epoch_violations = database.execute(
        """SELECT COUNT(*) FROM intent_fences AS fence
           LEFT JOIN intent_fence_epochs AS epoch ON epoch.intent_id=fence.intent_id
           WHERE epoch.intent_id IS NULL OR epoch.last_epoch < fence.fencing_epoch"""
    ).fetchone()[0]
    return not any(
        (
            owner_projection_violations,
            capture_projection_violations,
            owner_epoch_violations,
            intent_epoch_violations,
        )
    )


def _coordinator_v3_blackboard_invariant(database: sqlite3.Connection) -> bool:
    blackboard_epoch_violations = database.execute(
        """SELECT COUNT(*) FROM blackboard_claims AS claim
           LEFT JOIN blackboard_claim_epochs AS epoch
             ON epoch.project=claim.project AND epoch.resource=claim.resource
            AND epoch.last_epoch=claim.fencing_epoch
           WHERE epoch.project IS NULL"""
    ).fetchone()[0]
    blackboard_set_violations = database.execute(
        """SELECT COUNT(*) FROM blackboard_claims AS claim
           WHERE EXISTS (
               SELECT 1 FROM blackboard_claims AS sibling
                WHERE sibling.project=claim.project
                  AND sibling.claim_id=claim.claim_id
                  AND (
                      sibling.agent != claim.agent
                      OR sibling.lease_token != claim.lease_token
                      OR sibling.heartbeat_at != claim.heartbeat_at
                      OR sibling.expires_at != claim.expires_at
                  )
           )"""
    ).fetchone()[0]
    return not any((blackboard_epoch_violations, blackboard_set_violations))


def _coordinator_v3_cross_table_invariant(database: sqlite3.Connection) -> bool:
    return _coordinator_v3_base_cross_table_invariant(
        database
    ) and _coordinator_v3_blackboard_invariant(database)


def _require_v3_invariants(database: sqlite3.Connection) -> None:
    _require_v3_integrity(database)
    if _v3_operation_violations(database) or not _coordinator_v3_cross_table_invariant(
        database
    ):
        raise _coordinator_migration_error(
            "coordinator_v3_validation_failed",
            "coordinator v3 database invariant failed",
        )


def _require_v3_integrity(database: sqlite3.Connection) -> None:
    integrity = database.execute("PRAGMA integrity_check").fetchall()
    foreign_keys = database.execute("PRAGMA foreign_key_check").fetchall()
    if len(integrity) != 1 or integrity[0][0] != "ok":
        raise _coordinator_migration_error(
            "coordinator_v3_validation_failed",
            "coordinator v3 database invariant failed: SQLite integrity_check "
            "did not return ok",
        )
    if foreign_keys:
        raise _coordinator_migration_error(
            "coordinator_v3_validation_failed",
            f"coordinator v3 database invariant failed: foreign-key violations: {len(foreign_keys)}",
        )


def _v3_operation_violations(database: sqlite3.Connection) -> int:
    return database.execute(
        """SELECT COUNT(*) FROM "operation"
           WHERE position < 0
              OR parent_device IS NULL
              OR parent_inode IS NULL
              OR applied NOT IN (0,1)"""
    ).fetchone()[0]

# Preserve serialized helper identities through the original aliases.
_coordinator_migration_error.__module__ = "markdown_transaction"
_coordinator_v3_row_counts.__module__ = "markdown_transaction"
_coordinator_v3_base_cross_table_invariant.__module__ = "markdown_transaction"
_coordinator_v3_blackboard_invariant.__module__ = "markdown_transaction"
_coordinator_v3_cross_table_invariant.__module__ = "markdown_transaction"
_require_v3_invariants.__module__ = "markdown_transaction"
_require_v3_integrity.__module__ = "markdown_transaction"
_v3_operation_violations.__module__ = "markdown_transaction"
