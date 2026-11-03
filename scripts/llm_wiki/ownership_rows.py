"""Fenced ownership rows on a supplied connection; transaction control stays outside."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from llm_wiki.ownership_contracts import OperationalOwnershipError, OwnerRole, ProcessIdentity
from llm_wiki.ownership_timing import _timestamp


@dataclass(frozen=True)
class _AcquireRequest:
    """One validated acquisition, so the steps below share one vocabulary."""

    role: OwnerRole
    scope: str
    actor_id: str
    token: str
    process: ProcessIdentity
    marker_path: str | None
    marker_sha256: str | None
    marker_json: bytes | None


def _row_process(row: sqlite3.Row) -> ProcessIdentity:
    return ProcessIdentity(
        pid=row["process_id"], start_identity=row["process_start_identity"]
    )


def _next_owner_epoch(database: sqlite3.Connection, request: _AcquireRequest) -> int:
    epoch_row = database.execute(
        """INSERT INTO maintenance_owner_epochs(role,scope,last_epoch)
           VALUES (?,?,1)
           ON CONFLICT(role,scope) DO UPDATE
           SET last_epoch=maintenance_owner_epochs.last_epoch+1
           RETURNING last_epoch""",
        (request.role, request.scope),
    ).fetchone()
    if epoch_row is None:
        raise OperationalOwnershipError("owner_epoch_unavailable")
    return int(epoch_row[0])


def _insert_owner_row(
    database: sqlite3.Connection,
    request: _AcquireRequest,
    epoch: int,
    now: datetime,
    expires: datetime,
) -> None:
    try:
        result = database.execute(
            """INSERT INTO maintenance_owners(
                   role,scope,actor_id,owner_token,process_id,
                   process_start_identity,fencing_epoch,acquired_at,
                   heartbeat_at,expires_at,marker_path,marker_sha256,
                   marker_identity_json
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                request.role,
                request.scope,
                request.actor_id,
                request.token,
                request.process.pid,
                request.process.start_identity,
                epoch,
                _timestamp(now),
                _timestamp(now),
                _timestamp(expires),
                request.marker_path,
                request.marker_sha256,
                request.marker_json,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise OperationalOwnershipError("owner_identity_conflict") from exc
    if result.rowcount != 1:
        raise OperationalOwnershipError("owner_fence_lost")


# Every table whose rows exist only while a canonical owner does, named with
# the columns carrying that owner's fencing tuple. All three hold a real
# FOREIGN KEY to `maintenance_owners`, so a reclaim that leaves one behind
# cannot delete the owner at all, and each is also counted by the
# coordinator's cross-table shape invariant. Column order is `_owner_key`.
_OWNER_PROJECTIONS = (
    (
        "intent_fences",
        (
            "canonical_role",
            "canonical_scope",
            "canonical_actor_id",
            "canonical_owner_token",
            "canonical_fencing_epoch",
        ),
    ),
    (
        "project_leases",
        (
            "canonical_role",
            "canonical_scope",
            "actor_id",
            "lease_token",
            "fencing_epoch",
        ),
    ),
    (
        "writer_owners",
        (
            "canonical_role",
            "canonical_scope",
            "actor_id",
            "owner_token",
            "fencing_epoch",
        ),
    ),
)
_OWNER_PROJECTION_DELETES = tuple(
    "DELETE FROM {} WHERE {}".format(
        table, " AND ".join(f"{column}=?" for column in columns)
    )
    for table, columns in _OWNER_PROJECTIONS
)
# A binding projection must have the fence it was projected under, so it goes
# with that fence and only with that fence - exactly the pairing that
# `release_intent_fence` performs on the ordinary path.
_OWNER_BINDING_DELETE = """DELETE FROM capture_binding_projections
   WHERE EXISTS (
       SELECT 1 FROM intent_fences AS fence
        WHERE fence.intent_id=capture_binding_projections.intent_id
          AND fence.token=capture_binding_projections.intent_fence_token
          AND fence.fencing_epoch=capture_binding_projections.intent_fence_epoch
          AND fence.canonical_role=? AND fence.canonical_scope=?
          AND fence.canonical_actor_id=? AND fence.canonical_owner_token=?
          AND fence.canonical_fencing_epoch=?
   )"""


def _owner_key(row: sqlite3.Row) -> tuple[object, ...]:
    return (
        row["role"],
        row["scope"],
        row["actor_id"],
        row["owner_token"],
        row["fencing_epoch"],
    )


def _delete_owner_projections(database: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Remove the rows that exist only because this dead owner existed.

    Called only from the reclaim path, so the caller has already proved the
    lease expired and the process is gone. Every statement is scoped by the
    owner's whole fencing tuple, so it can reach nothing but rows naming this
    owner; a live owner's projection does not match and is not touched.
    """
    parameters = _owner_key(row)
    database.execute(_OWNER_BINDING_DELETE, parameters)
    for statement in _OWNER_PROJECTION_DELETES:
        database.execute(statement, parameters)

# Preserve historical serialized helper/value identities through old aliases.
_AcquireRequest.__module__ = "operational_ownership"
_row_process.__module__ = "operational_ownership"
_next_owner_epoch.__module__ = "operational_ownership"
_insert_owner_row.__module__ = "operational_ownership"
_owner_key.__module__ = "operational_ownership"
_delete_owner_projections.__module__ = "operational_ownership"
