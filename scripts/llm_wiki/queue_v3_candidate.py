"""Whether a built Reliability v3 queue database is sound enough to adopt."""
from __future__ import annotations

import re
import sqlite3

from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.formats.queue_payload import MAX_QUEUE_PAYLOAD_BYTES, validate_payload_blob
from llm_wiki.sqlite_contracts import migration_error


def ordinary_purge_authorization_digest(
    task_id: str, operation_id: str, manifest_sha256: str
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "manifest_sha256": manifest_sha256,
                "operation_id": operation_id,
                "task_id": task_id,
            }
        )
    )


def _ordinary_purge_manifest_digest(operation_id: object) -> str | None:
    prefix = "ordinary-purge:"
    if not isinstance(operation_id, str) or not operation_id.startswith(prefix):
        return None
    digest = operation_id.removeprefix(prefix)
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return None
    return digest


def _queue_v3_purge_authorization_valid(row: sqlite3.Row) -> bool:
    manifest_sha256 = _ordinary_purge_manifest_digest(row["operation_id"])
    if row["mode"] != "ordinary" or row["task_present"]:
        return False
    if manifest_sha256 is None:
        return False
    expected = ordinary_purge_authorization_digest(
        str(row["task_id"]), str(row["operation_id"]), manifest_sha256
    )
    return row["authorization_digest"] == expected


def _queue_v3_purge_authorizations_valid(database: sqlite3.Connection) -> bool:
    rows = database.execute(
        """SELECT authorization.*,
                  EXISTS(SELECT 1 FROM tasks WHERE id=authorization.task_id)
                    AS task_present
           FROM task_purge_authorizations AS authorization"""
    ).fetchall()
    return all(_queue_v3_purge_authorization_valid(row) for row in rows)


def _queue_v3_payloads_valid(database: sqlite3.Connection) -> bool:
    for row in database.execute(
        "SELECT payload_blob, input_hash, state, error_code FROM tasks"
    ):
        payload_bytes = bytes(row["payload_blob"])
        validation = validate_payload_blob(
            payload_bytes, row["input_hash"], parse=True
        )
        if validation.code is not None and (
            row["state"] != "dead" or row["error_code"] != "payload_hash_mismatch"
        ):
            return False
    return True


def _queue_v3_candidate_consistent(database: sqlite3.Connection) -> bool:
    """Whether the candidate agrees with itself: pages, keys and payloads."""
    integrity = database.execute("PRAGMA integrity_check").fetchall()
    if len(integrity) != 1 or integrity[0][0] != "ok":
        return False
    if database.execute("PRAGMA foreign_key_check").fetchall():
        return False
    return _queue_v3_payloads_valid(database)


def validate_queue_v3_candidate(database: sqlite3.Connection) -> None:
    """The candidate has to be internally consistent before it is published."""
    if not _queue_v3_candidate_consistent(database):
        raise migration_error(
            "queue_v3_validation_failed", "queue v3 candidate validation failed"
        )


def _queue_v3_payload_violations(database: sqlite3.Connection) -> int:
    """How many task rows hold a payload or attempt count out of contract."""
    return database.execute(
        """SELECT COUNT(*) FROM tasks
           WHERE typeof(payload_blob) != 'blob'
              OR length(payload_blob) > ?
              OR attempts NOT BETWEEN 0 AND 100""",
        (MAX_QUEUE_PAYLOAD_BYTES,),
    ).fetchone()[0]


def _queue_v3_structure_sound(database: sqlite3.Connection) -> bool:
    """Whether SQLite itself reports the file and its references intact."""
    integrity = database.execute("PRAGMA integrity_check").fetchall()
    if len(integrity) != 1 or integrity[0][0] != "ok":
        return False
    return not database.execute("PRAGMA foreign_key_check").fetchall()


def queue_v3_invariants_hold(database: sqlite3.Connection) -> bool:
    """Whether every invariant a queue v3 database has to keep still holds."""
    return (
        _queue_v3_structure_sound(database)
        and not _queue_v3_payload_violations(database)
        and _queue_v3_purge_authorizations_valid(database)
        and _queue_v3_payloads_valid(database)
    )
