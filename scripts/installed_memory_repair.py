"""Read-only inspection for the deferred Reliability V3 runtime adoption."""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import markdown_transaction
import memory_queue
from reliable_memory import (
    OperationalDatabaseContract,
    canonical_json_bytes,
    capture_runtime_file_identity,
    open_readonly_operational_db,
    read_runtime_bytes,
    sha256_bytes,
    validate_schema,
)

if TYPE_CHECKING:
    from operational_ownership import OwnerLease

_SCHEMA_DIR = Path(__file__).with_name("schemas")
_TOMBSTONE_SCHEMA = _SCHEMA_DIR / "operational-db-tombstone-v1.json"
_MIGRATION_SCHEMA = _SCHEMA_DIR / "reliability-v3-migration-v1.json"
_ADOPTION_SCHEMA = _SCHEMA_DIR / "reliability-v3-adoption-v1.json"
_MAX_TOMBSTONE_BYTES = 4 * 1024
_MAX_RECORD_BYTES = 64 * 1024
_MAX_OPERATION_ARTIFACTS = 32
_MAX_OPERATIONAL_DB_BYTES = 256 * 1024 * 1024
_MAX_OPERATIONAL_ROWS = 10_000
_MAX_RUNTIME_ENTRIES = 10_000
_MAX_CAPTURE_INTENT_BYTES = 1024 * 1024
_UNDO_RETENTION_DAYS = 30
_QUEUE_CONTRACT = OperationalDatabaseContract(application_id=0x4C575133)
_COORDINATOR_CONTRACT = OperationalDatabaseContract(application_id=0x4C575433)
_OPERATION_ARTIFACT_RE = re.compile(
    r"^\.reliability-v3-[0-9a-f]{64}-(?:queue|coordinator)"
    r"\.(?:candidate\.sqlite3|retired\.tmp)$"
)
_LEGACY_EVIDENCE = (
    "queue",
    "queue-migrated-v2",
    "transactions",
    "queue-results",
    "queue-quarantine",
    "capture-intents",
    "compile.pid",
    "maintenance.lock",
)


def _paths(state_root: Path) -> dict[str, Path]:
    run = state_root / "run"
    return {
        "run": run,
        "queue_legacy": run / "queue.sqlite3",
        "coordinator_legacy": run / "markdown-transactions.sqlite3",
        "queue_active": run / "queue-v3.sqlite3",
        "coordinator_active": run / "markdown-transactions-v3.sqlite3",
        "queue_candidate": run / "queue-v3.candidate.sqlite3",
        "coordinator_candidate": run / "markdown-transactions-v3.candidate.sqlite3",
        "queue_retired": run / "queue-v2-retired.sqlite3",
        "coordinator_retired": run / "markdown-transactions-v2-retired.sqlite3",
        "migration": run / "reliability-v3-migration.json",
        "adoption": run / "reliability-v3-adopted.json",
    }


def _kind(path: Path) -> str:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "missing"
    if stat.S_ISLNK(metadata.st_mode):
        return "link"
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    return "other"


def _nonempty_directory(path: Path) -> bool:
    try:
        with os.scandir(path) as entries:
            return next(entries, None) is not None
    except FileNotFoundError:
        return False


def _legacy_evidence(run: Path) -> list[str]:
    evidence: list[str] = []
    for name in _LEGACY_EVIDENCE:
        path = run / name
        kind = _kind(path)
        if kind == "file" or (kind == "directory" and _nonempty_directory(path)):
            evidence.append(f"run/{name}")
        elif kind not in {"missing", "directory"}:
            evidence.append(f"run/{name}:{kind}")
    return evidence


def _operation_artifacts(run: Path) -> tuple[list[str], bool]:
    if _kind(run) != "directory":
        return [], False
    names: list[str] = []
    with os.scandir(run) as entries:
        for entry in entries:
            if _OPERATION_ARTIFACT_RE.fullmatch(entry.name):
                names.append(entry.name)
                if len(names) > _MAX_OPERATION_ARTIFACTS:
                    return sorted(names[:_MAX_OPERATION_ARTIFACTS]), True
    return sorted(names), False


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _read_record(
    path: Path,
    state_root: Path,
    *,
    schema: Path,
    max_bytes: int,
) -> dict[str, object]:
    raw = read_runtime_bytes(
        path,
        state_root,
        max_bytes=max_bytes,
        owner_only=True,
    )
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("runtime record is not strict JSON") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        raise ValueError("runtime record is not canonical JSON")
    validate_schema(value, schema)
    return value


class ReliabilityV3ValidationError(RuntimeError):
    """Stable closed failure from Reliability V3 read-only validation."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _identity_value(identity: object) -> dict[str, object]:
    return {
        "platform": getattr(identity, "platform"),
        "volume": getattr(identity, "volume"),
        "file_id": getattr(identity, "file_id"),
        "size": getattr(identity, "size"),
        "mtime_ns": getattr(identity, "mtime_ns"),
    }


def _validate_artifact_reference(
    record: object,
    *,
    expected_path: Path,
    state_root: Path,
    mutable: bool,
    max_bytes: int,
) -> bytes | None:
    if not isinstance(record, dict):
        raise ValueError("artifact reference is not an object")
    relative = expected_path.relative_to(state_root).as_posix()
    if record.get("path") != relative:
        raise ValueError("artifact path differs from the fixed runtime path")
    actual_identity = capture_runtime_file_identity(expected_path, state_root=state_root)
    expected_identity = record.get("identity")
    if not isinstance(expected_identity, dict):
        raise ValueError("artifact identity is missing")
    actual_value = _identity_value(actual_identity)
    identity_fields = ("platform", "volume", "file_id") if mutable else tuple(actual_value)
    if any(expected_identity.get(field) != actual_value[field] for field in identity_fields):
        raise ValueError("artifact identity changed")
    if mutable:
        return None
    payload = read_runtime_bytes(
        expected_path,
        state_root,
        max_bytes=max_bytes,
        owner_only=True,
    )
    if record.get("sha256") != sha256_bytes(payload):
        raise ValueError("artifact digest changed")
    return payload


def _validate_active_database_reference(
    record: dict[str, object],
    *,
    database_name: str,
    path: Path,
    state_root: Path,
) -> None:
    _validate_artifact_reference(
        record.get("active"),
        expected_path=path,
        state_root=state_root,
        mutable=True,
        max_bytes=_MAX_OPERATIONAL_DB_BYTES,
    )
    contract = _QUEUE_CONTRACT if database_name == "queue" else _COORDINATOR_CONTRACT
    with contextlib.closing(
        open_readonly_operational_db(
            path,
            state_root,
            max_bytes=_MAX_OPERATIONAL_DB_BYTES,
            owner_only=True,
            contract=contract,
        )
    ) as database:
        complete = (
            memory_queue._queue_v3_schema_complete(database)
            if database_name == "queue"
            else markdown_transaction._coordinator_v3_schema_complete(database)
        )
        integrity = database.execute("PRAGMA integrity_check").fetchall()
        foreign_keys = database.execute("PRAGMA foreign_key_check").fetchall()
        observed = {
            "application_id": database.execute("PRAGMA application_id").fetchone()[0],
            "user_version": database.execute("PRAGMA user_version").fetchone()[0],
            "journal_mode": database.execute("PRAGMA journal_mode").fetchone()[0],
            "synchronous": database.execute("PRAGMA synchronous").fetchone()[0],
            "foreign_keys": database.execute("PRAGMA foreign_keys").fetchone()[0],
            "trusted_schema": database.execute("PRAGMA trusted_schema").fetchone()[0],
        }
    pragmas = record.get("pragmas")
    if (
        not complete
        or len(integrity) != 1
        or integrity[0][0] != "ok"
        or foreign_keys
        or record.get("application_id") != observed["application_id"]
        or record.get("user_version") != observed["user_version"]
        or not isinstance(pragmas, dict)
        or any(pragmas.get(key) != observed[key] for key in pragmas)
    ):
        raise ValueError("active database contract changed")


def _validate_complete_adoption(
    *,
    root: Path,
    state_root: Path,
    paths: dict[str, Path],
    migration: dict[str, object],
    adoption: dict[str, object],
    operation_artifacts: list[str],
) -> dict[str, object]:
    if operation_artifacts or any(
        _kind(paths[name]) != "missing"
        for name in ("queue_candidate", "coordinator_candidate")
    ):
        raise ValueError("candidate artifacts remain after adoption")
    if (
        adoption.get("operation_id") != migration.get("operation_id")
        or adoption.get("source_state") != migration.get("source_state")
        or adoption.get("schemas") != migration.get("schemas")
        or adoption.get("installed_integration_sha256")
        != migration.get("installed_integration_sha256")
    ):
        raise ValueError("adoption and migration records differ")
    migration_reference = adoption.get("migration")
    migration_bytes = read_runtime_bytes(
        paths["migration"],
        state_root,
        max_bytes=_MAX_RECORD_BYTES,
        owner_only=True,
    )
    if migration_reference != {
        "path": "run/reliability-v3-migration.json",
        "sha256": sha256_bytes(migration_bytes),
    }:
        raise ValueError("adoption migration reference changed")

    migration_databases = migration.get("databases")
    adoption_databases = adoption.get("databases")
    if not isinstance(migration_databases, list) or not isinstance(adoption_databases, list):
        raise ValueError("adoption database records are missing")
    migration_by_name = {item.get("database"): item for item in migration_databases}
    adoption_by_name = {item.get("database"): item for item in adoption_databases}
    if set(migration_by_name) != {"queue", "coordinator"} or set(adoption_by_name) != {
        "queue",
        "coordinator",
    }:
        raise ValueError("adoption database pair is incomplete")
    source_state = adoption["source_state"]
    expected = {
        "queue": (
            "run/queue.sqlite3",
            "run/queue-v3.sqlite3",
            "run/queue-v2-retired.sqlite3",
            "queue_legacy",
            "queue_active",
            "queue_retired",
        ),
        "coordinator": (
            "run/markdown-transactions.sqlite3",
            "run/markdown-transactions-v3.sqlite3",
            "run/markdown-transactions-v2-retired.sqlite3",
            "coordinator_legacy",
            "coordinator_active",
            "coordinator_retired",
        ),
    }
    schemas = adoption.get("schemas")
    if not isinstance(schemas, dict):
        raise ValueError("adoption schema references are missing")
    expected_schema_digests = {
        "queue_schema_sha256": memory_queue.QUEUE_V3_SCHEMA_SHA256,
        "coordinator_schema_sha256": (
            markdown_transaction.COORDINATOR_V3_SCHEMA_SHA256
        ),
        "adoption_schema_sha256": sha256_bytes(
            _ADOPTION_SCHEMA.read_bytes()
        ),
    }
    if schemas != expected_schema_digests:
        raise ValueError("adoption schema digests changed")
    integration_path = root / "scripts" / "integration_adapter.py"
    if adoption.get("installed_integration_sha256") != sha256_bytes(
        integration_path.read_bytes()
    ):
        raise ValueError("installed integration digest changed")
    for database_name in ("queue", "coordinator"):
        legacy, active, retired, legacy_key, active_key, retired_key = expected[database_name]
        migration_record = migration_by_name[database_name]
        adoption_record = adoption_by_name[database_name]
        if not isinstance(migration_record, dict) or not isinstance(adoption_record, dict):
            raise ValueError("adoption database record is invalid")
        expected_migration = {
            "database": database_name,
            "legacy_path": legacy,
            "replacement_path": active,
        }
        if source_state == "upgrade":
            expected_migration["retired_path"] = retired
            for field in ("source_identity", "source_sha256"):
                expected_migration[field] = migration_record.get(field)
        if migration_record != expected_migration:
            raise ValueError("migration database descriptor changed")
        tombstone_bytes = _validate_artifact_reference(
            adoption_record.get("tombstone"),
            expected_path=paths[legacy_key],
            state_root=state_root,
            mutable=False,
            max_bytes=_MAX_TOMBSTONE_BYTES,
        )
        tombstone = _read_record(
            paths[legacy_key],
            state_root,
            schema=_TOMBSTONE_SCHEMA,
            max_bytes=_MAX_TOMBSTONE_BYTES,
        )
        if tombstone_bytes != canonical_json_bytes(tombstone) or tombstone != {
            "schema_version": "operational-db-tombstone/v1",
            "database": database_name,
            "source_state": source_state,
            "legacy_path": legacy,
            "replacement_path": active,
            **(
                {
                    "retired_path": retired,
                    "retired_sha256": migration_record.get("source_sha256"),
                }
                if source_state == "upgrade"
                else {}
            ),
            "operation_id": adoption["operation_id"],
            "adoption_schema_sha256": schemas.get("adoption_schema_sha256"),
        }:
            raise ValueError("tombstone does not match adoption")
        _validate_active_database_reference(
            adoption_record,
            database_name=database_name,
            path=paths[active_key],
            state_root=state_root,
        )
        if source_state == "upgrade":
            retired_bytes = _validate_artifact_reference(
                adoption_record.get("retired"),
                expected_path=paths[retired_key],
                state_root=state_root,
                mutable=False,
                max_bytes=_MAX_OPERATIONAL_DB_BYTES,
            )
            if sha256_bytes(retired_bytes or b"") != migration_record.get("source_sha256"):
                raise ValueError("retired database differs from migration source")
        elif "retired" in adoption_record or _kind(paths[retired_key]) != "missing":
            raise ValueError("fresh adoption contains retired database evidence")
    return adoption


def require_reliability_v3_adopted(
    *, root: Path, state_root: Path
) -> dict[str, object]:
    """Return the validated complete adoption record or a stable closed error."""
    try:
        vault = Path(root).resolve(strict=True)
        state = Path(state_root).absolute()
        paths = _paths(state)
        operation_artifacts, overflow = _operation_artifacts(paths["run"])
        if overflow:
            raise ValueError("operation artifact scan exceeded its bound")
        if _kind(paths["migration"]) != "file" or _kind(paths["adoption"]) != "file":
            raise ReliabilityV3ValidationError("legacy_protocol_unquiesced")
        migration = _read_record(
            paths["migration"],
            state,
            schema=_MIGRATION_SCHEMA,
            max_bytes=_MAX_RECORD_BYTES,
        )
        adoption = _read_record(
            paths["adoption"],
            state,
            schema=_ADOPTION_SCHEMA,
            max_bytes=_MAX_RECORD_BYTES,
        )
        return _validate_complete_adoption(
            root=vault,
            state_root=state,
            paths=paths,
            migration=migration,
            adoption=adoption,
            operation_artifacts=operation_artifacts,
        )
    except ReliabilityV3ValidationError:
        raise
    except Exception as exc:
        raise ReliabilityV3ValidationError("reliability_v3_record_invalid") from exc


def _deadline_reached(deadline: float) -> bool:
    return time.monotonic() >= deadline


def _check_deadline(deadline: float) -> None:
    if _deadline_reached(deadline):
        raise TimeoutError("Reliability V3 validation deadline expired")


def _bounded_rows(
    database: sqlite3.Connection,
    query: str,
    *,
    deadline: float,
    parameters: tuple[object, ...] = (),
) -> list[sqlite3.Row]:
    _check_deadline(deadline)
    rows = database.execute(
        f"{query} LIMIT ?", (*parameters, _MAX_OPERATIONAL_ROWS + 1)
    ).fetchall()
    _check_deadline(deadline)
    if len(rows) > _MAX_OPERATIONAL_ROWS:
        raise ValueError("Reliability V3 row scan exceeded its bound")
    return rows


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("operational timestamp is not a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("operational timestamp lacks a timezone")
    return parsed.astimezone(timezone.utc)


def _owner_matches(row: sqlite3.Row, owner: OwnerLease | None) -> bool:
    return owner is not None and (
        row["role"],
        row["scope"],
        row["actor_id"],
        row["owner_token"],
        row["fencing_epoch"],
        row["process_id"],
        row["process_start_identity"],
    ) == (
        owner.role,
        owner.scope,
        owner.actor_id,
        owner.token,
        owner.epoch,
        owner.process.pid,
        owner.process.start_identity,
    )


def _bounded_entries(
    directory: Path,
    *,
    state_root: Path,
    deadline: float,
) -> list[Path]:
    _check_deadline(deadline)
    kind = _kind(directory)
    if kind == "missing":
        return []
    if kind != "directory":
        raise ValueError("runtime artifact root is not a directory")
    entries: list[Path] = []
    with os.scandir(directory) as scanned:
        for entry in scanned:
            _check_deadline(deadline)
            if len(entries) >= _MAX_RUNTIME_ENTRIES:
                raise ValueError("runtime artifact scan exceeded its bound")
            path = Path(entry.path)
            try:
                path.resolve(strict=True).relative_to(state_root.resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise PermissionError("runtime artifact escaped the state root") from exc
            entries.append(path)
    return entries


def validate_queue_v3_runtime(
    *,
    state_root: Path,
    now: datetime,
    deadline: float,
    excluded_owner: OwnerLease | None,
) -> list[str]:
    """Return bounded queue-v3 retention and consistency blockers."""
    del now
    path = Path(state_root) / "run" / "queue-v3.sqlite3"
    blockers: set[str] = set()
    try:
        with contextlib.closing(
            open_readonly_operational_db(
                path,
                state_root,
                max_bytes=_MAX_OPERATIONAL_DB_BYTES,
                owner_only=True,
                contract=_QUEUE_CONTRACT,
            )
        ) as database:
            tasks = _bounded_rows(database, "SELECT * FROM tasks", deadline=deadline)
            if tasks:
                blockers.add("queue_task_retained")
            for row in tasks:
                task_id = row["id"]
                payload = bytes(row["payload_blob"])
                if not isinstance(task_id, str) or not task_id:
                    raise ValueError("queue task ID is invalid")
                validation = memory_queue.validate_payload_blob(
                    payload, row["input_hash"], parse=True
                )
                if validation.code is not None and not (
                    row["state"] == "dead"
                    and row["error_code"] == "payload_hash_mismatch"
                ):
                    raise ValueError("queue task payload is invalid")
            table_codes = {
                "attempt_history": "queue_attempt_history_retained",
                "source_fences": "queue_source_fence_retained",
                "source_failures": "queue_source_failure_retained",
                "task_source_links": "queue_source_link_retained",
                "capture_intents": "capture_intent_retained",
                "capture_task_links": "capture_link_retained",
                "capture_task_link_resolutions": "capture_link_resolution_retained",
                "capture_task_link_seals": "capture_link_seal_retained",
                "semantic_decisions": "capture_semantic_decision_retained",
                "task_fences": "queue_task_fence_orphan",
                "task_purge_authorizations": "queue_purge_authorization_retained",
                "corrupt_export_operations": "queue_corrupt_export_retained",
                "corrupt_export_pages": "queue_corrupt_export_retained",
                "corrupt_dispositions": "queue_corrupt_disposition_retained",
                "corrupt_package_supersession_operations": "queue_corrupt_package_retained",
                "corrupt_package_supersession_pages": "queue_corrupt_package_retained",
                "corrupt_package_supersessions": "queue_corrupt_package_retained",
                "corrupt_purge_operations": "queue_corrupt_purge_retained",
                "corrupt_purge_pages": "queue_corrupt_purge_retained",
            }
            capture_intents: list[sqlite3.Row] = []
            for table, code in table_codes.items():
                rows = _bounded_rows(
                    database, f'SELECT * FROM "{table}"', deadline=deadline
                )
                if rows:
                    blockers.add(code)
                if table == "capture_intents":
                    capture_intents = rows
            for row in capture_intents:
                relative = row["relative_path"]
                if (
                    not isinstance(relative, str)
                    or not relative.startswith("run/capture-intents/")
                    or "\\" in relative
                    or ".." in Path(relative).parts
                ):
                    raise ValueError("capture intent path is invalid")
                payload = read_runtime_bytes(
                    Path(state_root) / relative,
                    state_root,
                    max_bytes=_MAX_CAPTURE_INTENT_BYTES,
                    owner_only=True,
                )
                if len(payload) != row["byte_size"] or sha256_bytes(payload) != row[
                    "intent_sha256"
                ]:
                    raise ValueError("capture intent evidence changed")
            owners = _bounded_rows(
                database, "SELECT * FROM queue_ownership", deadline=deadline
            )
            if owners:
                blockers.add("queue_owner_projection_orphan")
    except (OSError, PermissionError, sqlite3.Error, TimeoutError, ValueError) as exc:
        if isinstance(exc, TimeoutError):
            raise
        blockers.add("queue_state_unreadable")

    intent_entries = _bounded_entries(
        Path(state_root) / "run" / "capture-intents",
        state_root=Path(state_root),
        deadline=deadline,
    )
    if intent_entries:
        blockers.add("capture_intent_retained")
    for relative, code in (
        ("run/queue-results", "queue_result_retained"),
        ("run/queue-quarantine", "queue_quarantine_retained"),
    ):
        if _bounded_entries(
            Path(state_root) / relative,
            state_root=Path(state_root),
            deadline=deadline,
        ):
            blockers.add(code)
    del excluded_owner
    return sorted(blockers)


def validate_coordinator_v3_runtime(
    *,
    state_root: Path,
    now: datetime,
    deadline: float,
    excluded_owner: OwnerLease | None,
) -> list[str]:
    """Return bounded coordinator-v3 retention and consistency blockers."""
    path = Path(state_root) / "run" / "markdown-transactions-v3.sqlite3"
    blockers: set[str] = set()
    try:
        with contextlib.closing(
            open_readonly_operational_db(
                path,
                state_root,
                max_bytes=_MAX_OPERATIONAL_DB_BYTES,
                owner_only=True,
                contract=_COORDINATOR_CONTRACT,
            )
        ) as database:
            owners = _bounded_rows(
                database, "SELECT * FROM maintenance_owners", deadline=deadline
            )
            for row in owners:
                if not _owner_matches(row, excluded_owner):
                    blockers.add("canonical_owner_retained")
            for table, code in (
                ("project_leases", "project_lease_orphan"),
                ("writer_owners", "writer_owner_orphan"),
                ("intent_fences", "intent_fence_orphan"),
                ("capture_binding_projections", "capture_binding_projection_retained"),
            ):
                if _bounded_rows(
                    database, f'SELECT * FROM "{table}"', deadline=deadline
                ):
                    blockers.add(code)
            transactions = _bounded_rows(
                database, 'SELECT * FROM "transaction"', deadline=deadline
            )
            known_transaction_ids: set[str] = set()
            retained_transaction_ids: set[str] = set()
            cutoff = now.astimezone(timezone.utc) - timedelta(days=_UNDO_RETENTION_DAYS)
            for row in transactions:
                state = row["state"]
                transaction_id = row["id"]
                if not isinstance(transaction_id, str) or not transaction_id:
                    raise ValueError("transaction ID is invalid")
                known_transaction_ids.add(transaction_id)
                if state in {"preparing", "prepared", "applying", "aborting"}:
                    blockers.add("transaction_nonterminal")
                    retained_transaction_ids.add(transaction_id)
                elif state == "conflicted":
                    blockers.add("transaction_conflicted")
                    retained_transaction_ids.add(transaction_id)
                elif state == "quarantined":
                    blockers.add("transaction_quarantined")
                    retained_transaction_ids.add(transaction_id)
                elif state in {"committed", "aborted"}:
                    updated = _parse_timestamp(row["updated_at"])
                    if updated >= cutoff and row["artifacts_pruned_at"] is None:
                        blockers.add("transaction_undo_retained")
                        retained_transaction_ids.add(transaction_id)
                elif state != "discarded":
                    raise ValueError("transaction state is unknown")
                if state == "aborted":
                    receipt = (
                        Path(state_root)
                        / "run"
                        / "transactions"
                        / str(row["id"])
                        / "abort-receipt.json"
                    )
                    payload = read_runtime_bytes(
                        receipt,
                        state_root,
                        max_bytes=_MAX_RECORD_BYTES,
                        owner_only=True,
                    )
                    if sha256_bytes(payload) != row["abort_receipt_sha256"]:
                        raise ValueError("aborted transaction receipt changed")
    except (OSError, PermissionError, sqlite3.Error, TimeoutError, ValueError) as exc:
        if isinstance(exc, TimeoutError):
            raise
        blockers.add("transaction_state_unreadable")
    artifact_entries = _bounded_entries(
        Path(state_root) / "run" / "transactions",
        state_root=Path(state_root),
        deadline=deadline,
    )
    artifact_ids: set[str] = set()
    for entry in artifact_entries:
        if _kind(entry) != "directory" or re.fullmatch(r"[0-9a-z_-]{1,128}", entry.name) is None:
            blockers.add("transaction_artifact_state_unknown")
            continue
        artifact_ids.add(entry.name)
    if retained_transaction_ids & artifact_ids:
        blockers.add("transaction_artifact_retained")
    if artifact_ids - known_transaction_ids:
        blockers.add("transaction_artifact_state_unknown")
    return sorted(blockers)


def validate_reliability_v3_runtime(
    *,
    root: Path,
    state_root: Path,
    now: datetime,
    deadline: float,
    excluded_owner: OwnerLease | None,
) -> list[str]:
    """Validate adopted operational state without mutating or acquiring ownership."""
    del root
    blockers = set(
        validate_queue_v3_runtime(
            state_root=state_root,
            now=now,
            deadline=deadline,
            excluded_owner=excluded_owner,
        )
    )
    blockers.update(
        validate_coordinator_v3_runtime(
            state_root=state_root,
            now=now,
            deadline=deadline,
            excluded_owner=excluded_owner,
        )
    )
    paths = _paths(Path(state_root))
    if any(_kind(paths[name]) != "missing" for name in ("queue_retired", "coordinator_retired")):
        blockers.add("retired_operational_database_retained")
    for path, code in (
        (Path(state_root) / "run" / "compile.pid", "compile_marker_retained"),
        (Path(state_root) / "run" / "maintenance.lock", "maintenance_marker_retained"),
    ):
        if _kind(path) != "missing":
            blockers.add(code)
    _check_deadline(deadline)
    return sorted(blockers)


def _validate_legacy_database(path: Path, state_root: Path) -> None:
    with contextlib.closing(
        sqlite3.connect(
            f"{path.resolve(strict=True).as_uri()}?mode=ro",
            uri=True,
            timeout=0,
            isolation_level=None,
        )
    ) as database:
        database.execute("PRAGMA query_only=ON")
        database.execute("SELECT name FROM sqlite_schema LIMIT 1").fetchall()
    if not path.resolve(strict=True).is_relative_to(state_root.resolve(strict=True)):
        raise PermissionError("legacy database is outside the state root")


def _validate_partial_artifacts(
    paths: dict[str, Path],
    state_root: Path,
    migration: dict[str, object],
    operation_artifacts: list[str],
) -> None:
    source_state = migration["source_state"]
    for name in ("queue_active", "coordinator_active"):
        if _kind(paths[name]) == "file":
            if name == "queue_active":
                memory_queue.validate_queue_v3_database(paths[name], state_root=state_root)
            else:
                markdown_transaction.validate_coordinator_v3_database(
                    paths[name], state_root=state_root
                )
        elif _kind(paths[name]) != "missing":
            raise ValueError("v3 active database path has the wrong kind")
    for name in ("queue_legacy", "coordinator_legacy"):
        if _kind(paths[name]) == "file":
            _read_record(
                paths[name],
                state_root,
                schema=_TOMBSTONE_SCHEMA,
                max_bytes=_MAX_TOMBSTONE_BYTES,
            )
        elif _kind(paths[name]) != "missing":
            raise ValueError("legacy database path has the wrong kind")
    if source_state == "fresh" and any(
        _kind(paths[name]) != "missing"
        for name in ("queue_retired", "coordinator_retired")
    ):
        raise ValueError("fresh migration has retired databases")
    for name in operation_artifacts:
        candidate = paths["run"] / name
        if name.endswith("queue.candidate.sqlite3"):
            memory_queue.validate_queue_v3_database(candidate, state_root=state_root)
        elif name.endswith("coordinator.candidate.sqlite3"):
            markdown_transaction.validate_coordinator_v3_database(
                candidate, state_root=state_root
            )


def _report(
    *,
    mode: Literal["check", "apply"],
    status: Literal["degraded", "error"],
    state: str,
    blockers: list[str],
    artifacts: dict[str, object] | None = None,
) -> dict[str, object]:
    details: dict[str, object] = {"adoption_state": state}
    if artifacts is not None:
        details["artifacts"] = artifacts
    return {
        "mode": mode,
        "overall_status": status,
        "actions": [],
        "blockers": [{"code": code} for code in blockers],
        "details": details,
    }


def inspect_installed_vault(*, root: Path, state_root: Path) -> dict[str, object]:
    """Run bounded Reliability V3 validation without creating or mutating state."""
    try:
        vault = Path(root).resolve(strict=True)
        if not vault.is_dir():
            raise ValueError("vault root is not a directory")
        state = Path(state_root).absolute()
        paths = _paths(state)
        kinds = {
            name: _kind(path)
            for name, path in paths.items()
            if name != "run" and _kind(path) != "missing"
        }
        operation_artifacts, operation_overflow = _operation_artifacts(paths["run"])
        legacy_evidence = _legacy_evidence(paths["run"])
        v3_present = any(
            name in kinds
            for name in (
                "queue_active",
                "coordinator_active",
                "queue_retired",
                "coordinator_retired",
                "migration",
                "adoption",
            )
        ) or bool(operation_artifacts) or operation_overflow

        if not v3_present:
            pair = (
                _kind(paths["queue_legacy"]),
                _kind(paths["coordinator_legacy"]),
            )
            if pair == ("missing", "missing"):
                if legacy_evidence:
                    return _report(
                        mode="check",
                        status="error",
                        state="conflict",
                        blockers=["legacy_operational_evidence_present"],
                        artifacts={"legacy_evidence": legacy_evidence},
                    )
                return _report(
                    mode="check",
                    status="degraded",
                    state="fresh",
                    blockers=["reliability_v3_runtime_activation_incomplete"],
                    artifacts={},
                )
            if pair == ("file", "file"):
                _validate_legacy_database(paths["queue_legacy"], state)
                _validate_legacy_database(paths["coordinator_legacy"], state)
                return _report(
                    mode="check",
                    status="degraded",
                    state="upgrade-required",
                    blockers=["reliability_v3_runtime_activation_incomplete"],
                    artifacts={"paths": kinds},
                )
            return _report(
                mode="check",
                status="error",
                state="conflict",
                blockers=["legacy_operational_database_pair_incomplete"],
                artifacts={"paths": kinds},
            )

        if operation_overflow:
            return _report(
                mode="check",
                status="error",
                state="conflict",
                blockers=["reliability_v3_operation_artifact_limit_exceeded"],
                artifacts={"operation_artifacts": operation_artifacts},
            )
        if _kind(paths["migration"]) != "file":
            return _report(
                mode="check",
                status="error",
                state="conflict",
                blockers=["reliability_v3_migration_record_missing"],
                artifacts={"paths": kinds, "operation_artifacts": operation_artifacts},
            )
        migration = _read_record(
            paths["migration"],
            state,
            schema=_MIGRATION_SCHEMA,
            max_bytes=_MAX_RECORD_BYTES,
        )
        _validate_partial_artifacts(paths, state, migration, operation_artifacts)
        if _kind(paths["adoption"]) == "missing":
            return _report(
                mode="check",
                status="degraded",
                state="partial",
                blockers=["reliability_v3_runtime_activation_incomplete"],
                artifacts={"paths": kinds, "operation_artifacts": operation_artifacts},
            )
        if _kind(paths["adoption"]) != "file":
            raise ValueError("adoption record path has the wrong kind")
        adoption = _read_record(
            paths["adoption"],
            state,
            schema=_ADOPTION_SCHEMA,
            max_bytes=_MAX_RECORD_BYTES,
        )
        _validate_complete_adoption(
            root=vault,
            state_root=state,
            paths=paths,
            migration=migration,
            adoption=adoption,
            operation_artifacts=operation_artifacts,
        )
        return _report(
            mode="check",
            status="degraded",
            state="adopted",
            blockers=["reliability_v3_runtime_activation_incomplete"],
            artifacts={"paths": kinds, "operation_artifacts": operation_artifacts},
        )
    except Exception:  # noqa: BLE001 - closed read-only inspection envelope
        return _report(
            mode="check",
            status="error",
            state="conflict",
            blockers=["reliability_v3_record_invalid"],
        )


def repair_installed_vault(
    *,
    root: Path,
    state_root: Path,
    adopt_ownership_v3: bool,
    confirm_all_agents_stopped: bool,
) -> dict[str, object]:
    """Fail closed until compatible v3 writers and ownership are activated."""
    del adopt_ownership_v3, confirm_all_agents_stopped
    inspected = inspect_installed_vault(root=root, state_root=state_root)
    return _report(
        mode="apply",
        status="error",
        state=str(inspected["details"].get("adoption_state", "conflict")),
        blockers=["reliability_v3_runtime_activation_incomplete"],
    )
