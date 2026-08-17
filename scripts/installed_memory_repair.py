"""Read-only inspection for the deferred Reliability V3 runtime adoption."""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import stat
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple

import markdown_transaction
import memory_queue
from bounded_io import read_stable_bytes
from install_control import validate_install_state
from reliable_memory import (
    OperationalDatabaseContract,
    canonical_json_bytes,
    capture_runtime_file_identity,
    durable_publish_file,
    open_readonly_operational_db,
    publish_runtime_file,
    read_runtime_bytes,
    sha256_bytes,
    sync_runtime_directory,
    validate_schema,
    validate_state_root,
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
_MAX_INTEGRATION_BYTES = 16 * 1024 * 1024


class _DatabaseSpec(NamedTuple):
    name: str
    legacy_key: str
    active_key: str
    retired_key: str
    legacy_path: str
    active_path: str
    retired_path: str
    initialize: Callable[..., dict[str, int]]
    validate: Callable[..., dict[str, object]]


_DATABASE_SPECS = (
    _DatabaseSpec(
        "queue",
        "queue_legacy",
        "queue_active",
        "queue_retired",
        "run/queue.sqlite3",
        "run/queue-v3.sqlite3",
        "run/queue-v2-retired.sqlite3",
        memory_queue.initialize_queue_v3_candidate,
        memory_queue.validate_queue_v3_database,
    ),
    _DatabaseSpec(
        "coordinator",
        "coordinator_legacy",
        "coordinator_active",
        "coordinator_retired",
        "run/markdown-transactions.sqlite3",
        "run/markdown-transactions-v3.sqlite3",
        "run/markdown-transactions-v2-retired.sqlite3",
        markdown_transaction.initialize_coordinator_v3_candidate,
        markdown_transaction.validate_coordinator_v3_database,
    ),
)


def _install_deletion_blockers(state_root: Path) -> list[str]:
    result = validate_install_state(state_root)
    codes = result.get("deletion_codes")
    if not isinstance(codes, list):
        return ["install_state_corrupt"]
    if any(not isinstance(code, str) for code in codes):
        return ["install_state_corrupt"]
    return codes


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
    evidence = (_legacy_evidence_item(run, name) for name in _LEGACY_EVIDENCE)
    return [item for item in evidence if item is not None]


def _legacy_evidence_item(run: Path, name: str) -> str | None:
    kind = _kind(run / name)
    if kind == "file":
        return f"run/{name}"
    if kind == "directory":
        return f"run/{name}" if _nonempty_directory(run / name) else None
    if kind == "missing":
        return None
    return f"run/{name}:{kind}"


def _operation_artifacts(run: Path) -> tuple[list[str], bool]:
    if _kind(run) != "directory":
        return [], False
    with os.scandir(run) as entries:
        names = [
            entry.name
            for entry in entries
            if _OPERATION_ARTIFACT_RE.fullmatch(entry.name)
        ]
    names.sort()
    return names[:_MAX_OPERATION_ARTIFACTS], len(names) > _MAX_OPERATION_ARTIFACTS


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
    artifact = _require_artifact_record(record, expected_path, state_root)
    actual_identity = capture_runtime_file_identity(expected_path, state_root=state_root)
    _validate_artifact_identity(artifact, actual_identity, mutable)
    if mutable:
        return None
    return _validate_immutable_artifact(
        artifact,
        expected_path=expected_path,
        state_root=state_root,
        max_bytes=max_bytes,
    )


def _require_artifact_record(
    record: object, expected_path: Path, state_root: Path
) -> dict[str, object]:
    if not isinstance(record, dict):
        raise ValueError("artifact reference is not an object")
    relative = expected_path.relative_to(state_root).as_posix()
    if record.get("path") != relative:
        raise ValueError("artifact path differs from the fixed runtime path")
    return record


def _validate_artifact_identity(
    record: dict[str, object], actual_identity: object, mutable: bool
) -> None:
    expected_identity = record.get("identity")
    if not isinstance(expected_identity, dict):
        raise ValueError("artifact identity is missing")
    actual_value = _identity_value(actual_identity)
    identity_fields = tuple(actual_value)
    if mutable:
        identity_fields = ("platform", "volume", "file_id")
    if any(expected_identity.get(field) != actual_value[field] for field in identity_fields):
        raise ValueError("artifact identity changed")


def _validate_immutable_artifact(
    record: dict[str, object],
    *,
    expected_path: Path,
    state_root: Path,
    max_bytes: int,
) -> bytes:
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
    contract = _database_contract(database_name)
    with contextlib.closing(
        open_readonly_operational_db(
            path,
            state_root,
            max_bytes=_MAX_OPERATIONAL_DB_BYTES,
            owner_only=True,
            contract=contract,
        )
    ) as database:
        complete = _database_schema_complete(database_name, database)
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
    _require_database_health(complete, integrity, foreign_keys)
    _require_database_metadata(record, observed)


def _database_contract(database_name: str) -> OperationalDatabaseContract:
    if database_name == "queue":
        return _QUEUE_CONTRACT
    if database_name == "coordinator":
        return _COORDINATOR_CONTRACT
    raise ValueError("unknown operational database")


def _database_schema_complete(database_name: str, database: sqlite3.Connection) -> bool:
    if database_name == "queue":
        return memory_queue._queue_v3_schema_complete(database)
    if database_name == "coordinator":
        return markdown_transaction._coordinator_v3_schema_complete(database)
    raise ValueError("unknown operational database")


def _require_database_health(
    complete: bool, integrity: list[sqlite3.Row], foreign_keys: list[sqlite3.Row]
) -> None:
    if not complete:
        raise ValueError("active database schema is incomplete")
    if len(integrity) != 1 or integrity[0][0] != "ok":
        raise ValueError("active database integrity check failed")
    if foreign_keys:
        raise ValueError("active database foreign key check failed")


def _require_database_metadata(
    record: dict[str, object], observed: dict[str, object]
) -> None:
    if record.get("application_id") != observed["application_id"]:
        raise ValueError("active database application ID changed")
    if record.get("user_version") != observed["user_version"]:
        raise ValueError("active database version changed")
    _require_database_pragmas(record.get("pragmas"), observed)


def _require_database_pragmas(pragmas: object, observed: dict[str, object]) -> None:
    if not isinstance(pragmas, dict):
        raise ValueError("active database PRAGMAs are missing")
    if any(pragmas.get(key) != observed[key] for key in pragmas):
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
    _require_adoption_artifacts_complete(paths, operation_artifacts)
    _require_adoption_header(adoption, migration)
    _require_migration_reference(adoption, paths, state_root)
    migration_by_name = _named_database_records(migration.get("databases"))
    adoption_by_name = _named_database_records(adoption.get("databases"))
    schemas = _require_adoption_sources(root, adoption)
    for database_name in ("queue", "coordinator"):
        _validate_adopted_database(
            database_name=database_name,
            source_state=str(adoption["source_state"]),
            operation_id=str(adoption["operation_id"]),
            schemas=schemas,
            paths=paths,
            state_root=state_root,
            migration_record=migration_by_name[database_name],
            adoption_record=adoption_by_name[database_name],
        )
    return adoption


def _require_adoption_artifacts_complete(
    paths: dict[str, Path], operation_artifacts: list[str]
) -> None:
    if operation_artifacts:
        raise ValueError("operation artifacts remain after adoption")
    if any(
        _kind(paths[name]) != "missing"
        for name in ("queue_candidate", "coordinator_candidate")
    ):
        raise ValueError("candidate artifacts remain after adoption")


def _require_adoption_header(
    adoption: dict[str, object], migration: dict[str, object]
) -> None:
    fields = ("operation_id", "source_state", "schemas", "installed_integration_sha256")
    if any(adoption.get(field) != migration.get(field) for field in fields):
        raise ValueError("adoption and migration records differ")


def _require_migration_reference(
    adoption: dict[str, object], paths: dict[str, Path], state_root: Path
) -> None:
    migration_bytes = read_runtime_bytes(
        paths["migration"], state_root, max_bytes=_MAX_RECORD_BYTES, owner_only=True
    )
    expected = {
        "path": "run/reliability-v3-migration.json",
        "sha256": sha256_bytes(migration_bytes),
    }
    if adoption.get("migration") != expected:
        raise ValueError("adoption migration reference changed")


def _named_database_records(value: object) -> dict[str, dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError("adoption database records are missing")
    result: dict[str, dict[str, object]] = {}
    for item in value:
        name, record = _named_database_record(item, result)
        result[name] = record
    if set(result) != {"queue", "coordinator"}:
        raise ValueError("adoption database pair is incomplete")
    return result


def _named_database_record(
    item: object, existing: dict[str, dict[str, object]]
) -> tuple[str, dict[str, object]]:
    if not isinstance(item, dict):
        raise ValueError("adoption database record is invalid")
    name = item.get("database")
    if not isinstance(name, str):
        raise ValueError("adoption database name is invalid")
    if name in existing:
        raise ValueError("adoption database name is duplicated")
    return name, item


def _expected_schema_digests() -> dict[str, str]:
    return {
        "queue_schema_sha256": memory_queue.QUEUE_V3_SCHEMA_SHA256,
        "coordinator_schema_sha256": markdown_transaction.COORDINATOR_V3_SCHEMA_SHA256,
        "adoption_schema_sha256": sha256_bytes(_ADOPTION_SCHEMA.read_bytes()),
    }


def _require_adoption_sources(
    root: Path, adoption: dict[str, object]
) -> dict[str, object]:
    schemas = adoption.get("schemas")
    if not isinstance(schemas, dict) or schemas != _expected_schema_digests():
        raise ValueError("adoption schema digests changed")
    integration = (root / "scripts" / "integration_adapter.py").read_bytes()
    if adoption.get("installed_integration_sha256") != sha256_bytes(integration):
        raise ValueError("installed integration digest changed")
    return schemas


def _database_spec(database_name: str) -> tuple[str, str, str, str, str, str]:
    if database_name == "queue":
        return (
            "run/queue.sqlite3",
            "run/queue-v3.sqlite3",
            "run/queue-v2-retired.sqlite3",
            "queue_legacy",
            "queue_active",
            "queue_retired",
        )
    if database_name == "coordinator":
        return (
            "run/markdown-transactions.sqlite3",
            "run/markdown-transactions-v3.sqlite3",
            "run/markdown-transactions-v2-retired.sqlite3",
            "coordinator_legacy",
            "coordinator_active",
            "coordinator_retired",
        )
    raise ValueError("unknown operational database")


def _expected_migration_record(
    *, database_name: str, source_state: str, migration_record: dict[str, object]
) -> dict[str, object]:
    legacy, active, retired, _legacy_key, _active_key, _retired_key = _database_spec(
        database_name
    )
    expected = {
        "database": database_name,
        "legacy_path": legacy,
        "replacement_path": active,
    }
    if source_state == "upgrade":
        expected.update(
            {
                "retired_path": retired,
                "source_identity": migration_record.get("source_identity"),
                "source_sha256": migration_record.get("source_sha256"),
            }
        )
    return expected


def _expected_tombstone(
    *,
    database_name: str,
    source_state: str,
    operation_id: str,
    adoption_schema_sha256: object,
    migration_record: dict[str, object],
) -> dict[str, object]:
    legacy, active, retired, _legacy_key, _active_key, _retired_key = _database_spec(
        database_name
    )
    expected = {
        "schema_version": "operational-db-tombstone/v1",
        "database": database_name,
        "source_state": source_state,
        "legacy_path": legacy,
        "replacement_path": active,
        "operation_id": operation_id,
        "adoption_schema_sha256": adoption_schema_sha256,
    }
    if source_state == "upgrade":
        expected["retired_path"] = retired
        expected["retired_sha256"] = migration_record.get("source_sha256")
    return expected


def _validate_adopted_database(
    *,
    database_name: str,
    source_state: str,
    operation_id: str,
    schemas: dict[str, object],
    paths: dict[str, Path],
    state_root: Path,
    migration_record: dict[str, object],
    adoption_record: dict[str, object],
) -> None:
    expected_migration = _expected_migration_record(
        database_name=database_name,
        source_state=source_state,
        migration_record=migration_record,
    )
    if migration_record != expected_migration:
        raise ValueError("migration database descriptor changed")
    _validate_adopted_tombstone(
        database_name=database_name,
        source_state=source_state,
        operation_id=operation_id,
        schemas=schemas,
        paths=paths,
        state_root=state_root,
        migration_record=migration_record,
        adoption_record=adoption_record,
    )
    _validate_adopted_active(database_name, paths, state_root, adoption_record)
    _validate_adopted_retired(
        database_name, source_state, paths, state_root, migration_record, adoption_record
    )


def _validate_adopted_tombstone(
    *,
    database_name: str,
    source_state: str,
    operation_id: str,
    schemas: dict[str, object],
    paths: dict[str, Path],
    state_root: Path,
    migration_record: dict[str, object],
    adoption_record: dict[str, object],
) -> None:
    _legacy, _active, _retired, legacy_key, _active_key, _retired_key = _database_spec(
        database_name
    )
    tombstone_bytes = _validate_artifact_reference(
        adoption_record.get("tombstone"),
        expected_path=paths[legacy_key],
        state_root=state_root,
        mutable=False,
        max_bytes=_MAX_TOMBSTONE_BYTES,
    )
    tombstone = _read_record(
        paths[legacy_key], state_root, schema=_TOMBSTONE_SCHEMA, max_bytes=_MAX_TOMBSTONE_BYTES
    )
    if tombstone_bytes != canonical_json_bytes(tombstone):
        raise ValueError("tombstone bytes are not canonical")
    expected = _expected_tombstone(
        database_name=database_name,
        source_state=source_state,
        operation_id=operation_id,
        adoption_schema_sha256=schemas.get("adoption_schema_sha256"),
        migration_record=migration_record,
    )
    if tombstone != expected:
        raise ValueError("tombstone does not match adoption")


def _validate_adopted_active(
    database_name: str,
    paths: dict[str, Path],
    state_root: Path,
    adoption_record: dict[str, object],
) -> None:
    _legacy, _active, _retired, _legacy_key, active_key, _retired_key = _database_spec(
        database_name
    )
    _validate_active_database_reference(
        adoption_record,
        database_name=database_name,
        path=paths[active_key],
        state_root=state_root,
    )


def _validate_adopted_retired(
    database_name: str,
    source_state: str,
    paths: dict[str, Path],
    state_root: Path,
    migration_record: dict[str, object],
    adoption_record: dict[str, object],
) -> None:
    _legacy, _active, _retired, _legacy_key, _active_key, retired_key = _database_spec(
        database_name
    )
    if source_state == "upgrade":
        _validate_upgrade_retired(
            paths[retired_key], state_root, migration_record, adoption_record
        )
        return
    if "retired" in adoption_record or _kind(paths[retired_key]) != "missing":
        raise ValueError("fresh adoption contains retired database evidence")


def _validate_upgrade_retired(
    path: Path,
    state_root: Path,
    migration_record: dict[str, object],
    adoption_record: dict[str, object],
) -> None:
    retired_bytes = _validate_artifact_reference(
        adoption_record.get("retired"),
        expected_path=path,
        state_root=state_root,
        mutable=False,
        max_bytes=_MAX_OPERATIONAL_DB_BYTES,
    )
    if sha256_bytes(retired_bytes or b"") != migration_record.get("source_sha256"):
        raise ValueError("retired database differs from migration source")


def require_reliability_v3_adopted(
    *, root: Path, state_root: Path
) -> dict[str, object]:
    """Return the validated complete adoption record or a stable closed error."""
    try:
        return _load_complete_adoption(root=Path(root), state_root=Path(state_root))
    except ReliabilityV3ValidationError:
        raise
    except Exception as exc:
        raise ReliabilityV3ValidationError("reliability_v3_record_invalid") from exc


def _load_complete_adoption(*, root: Path, state_root: Path) -> dict[str, object]:
    vault = root.resolve(strict=True)
    state = state_root.absolute()
    paths = _paths(state)
    operation_artifacts, overflow = _operation_artifacts(paths["run"])
    if overflow:
        raise ValueError("operation artifact scan exceeded its bound")
    if _kind(paths["migration"]) != "file" or _kind(paths["adoption"]) != "file":
        raise ReliabilityV3ValidationError("legacy_protocol_unquiesced")
    migration = _read_record(
        paths["migration"], state, schema=_MIGRATION_SCHEMA, max_bytes=_MAX_RECORD_BYTES
    )
    adoption = _read_record(
        paths["adoption"], state, schema=_ADOPTION_SCHEMA, max_bytes=_MAX_RECORD_BYTES
    )
    return _validate_complete_adoption(
        root=vault,
        state_root=state,
        paths=paths,
        migration=migration,
        adoption=adoption,
        operation_artifacts=operation_artifacts,
    )


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
    return _scan_bounded_entries(directory, state_root=state_root, deadline=deadline)


def _scan_bounded_entries(
    directory: Path, *, state_root: Path, deadline: float
) -> list[Path]:
    entries: list[Path] = []
    with os.scandir(directory) as scanned:
        for entry in scanned:
            _check_deadline(deadline)
            if len(entries) >= _MAX_RUNTIME_ENTRIES:
                raise ValueError("runtime artifact scan exceeded its bound")
            entries.append(_contained_runtime_entry(entry.path, state_root))
    return entries


def _contained_runtime_entry(value: str, state_root: Path) -> Path:
    path = Path(value)
    try:
        path.resolve(strict=True).relative_to(state_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise PermissionError("runtime artifact escaped the state root") from exc
    return path


_QUEUE_TABLE_CODES = {
    "attempt_history": "queue_attempt_history_retained",
    "source_fences": "queue_source_fence_retained",
    "source_failures": "queue_source_failure_retained",
    "task_source_links": "queue_source_link_retained",
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


def validate_queue_v3_runtime(
    *,
    state_root: Path,
    now: datetime,
    deadline: float,
    excluded_owner: OwnerLease | None,
) -> list[str]:
    """Return bounded queue-v3 retention and consistency blockers."""
    del now, excluded_owner
    blockers: set[str] = set()
    try:
        blockers.update(_queue_database_blockers(Path(state_root), deadline))
        blockers.update(_queue_artifact_blockers(Path(state_root), deadline))
    except TimeoutError:
        raise
    except (OSError, PermissionError, sqlite3.Error, ValueError):
        blockers.add("queue_state_unreadable")
    return sorted(blockers)


def _queue_database_blockers(state_root: Path, deadline: float) -> set[str]:
    path = state_root / "run" / "queue-v3.sqlite3"
    with contextlib.closing(
        open_readonly_operational_db(
            path,
            state_root,
            max_bytes=_MAX_OPERATIONAL_DB_BYTES,
            owner_only=True,
            contract=_QUEUE_CONTRACT,
        )
    ) as database:
        blockers = _queue_task_blockers(database, deadline)
        blockers.update(_queue_table_blockers(database, deadline))
        blockers.update(_capture_intent_blockers(database, state_root, deadline))
        if _bounded_rows(database, "SELECT * FROM queue_ownership", deadline=deadline):
            blockers.add("queue_owner_projection_orphan")
    return blockers


def _queue_task_blockers(
    database: sqlite3.Connection, deadline: float
) -> set[str]:
    rows = _bounded_rows(database, "SELECT * FROM tasks", deadline=deadline)
    blockers: set[str] = set()
    if rows:
        blockers.add("queue_task_retained")
    for row in rows:
        _validate_queue_task(row)
    return blockers


def _validate_queue_task(row: sqlite3.Row) -> None:
    _require_task_id(row["id"])
    validation = memory_queue.validate_payload_blob(
        bytes(row["payload_blob"]), row["input_hash"], parse=True
    )
    if validation.code is None:
        return
    _require_expected_hash_failure(row)


def _require_task_id(value: object) -> None:
    if not isinstance(value, str):
        raise ValueError("queue task ID is invalid")
    if not value:
        raise ValueError("queue task ID is invalid")


def _require_expected_hash_failure(row: sqlite3.Row) -> None:
    if row["state"] != "dead":
        raise ValueError("queue task payload is invalid")
    if row["error_code"] != "payload_hash_mismatch":
        raise ValueError("queue task payload is invalid")


_COORDINATOR_TABLE_CODES = (
    ("project_leases", "project_lease_orphan"),
    ("writer_owners", "writer_owner_orphan"),
    ("intent_fences", "intent_fence_orphan"),
    ("capture_binding_projections", "capture_binding_projection_retained"),
)

_TRANSACTION_STATE_CODES = {
    "preparing": "transaction_nonterminal",
    "prepared": "transaction_nonterminal",
    "applying": "transaction_nonterminal",
    "aborting": "transaction_nonterminal",
    "conflicted": "transaction_conflicted",
    "quarantined": "transaction_quarantined",
}


def _queue_table_blockers(
    database: sqlite3.Connection, deadline: float
) -> set[str]:
    blockers: set[str] = set()
    for table, code in _QUEUE_TABLE_CODES.items():
        rows = _bounded_rows(database, f'SELECT * FROM "{table}"', deadline=deadline)
        if rows:
            blockers.add(code)
    return blockers


def _capture_intent_blockers(
    database: sqlite3.Connection, state_root: Path, deadline: float
) -> set[str]:
    rows = _bounded_rows(database, "SELECT * FROM capture_intents", deadline=deadline)
    blockers: set[str] = set()
    if rows:
        blockers.add("capture_intent_retained")
    for row in rows:
        _validate_capture_intent(row, state_root)
    return blockers


def _validate_capture_intent(row: sqlite3.Row, state_root: Path) -> None:
    relative = row["relative_path"]
    if not _valid_capture_path(relative):
        raise ValueError("capture intent path is invalid")
    payload = read_runtime_bytes(
        state_root / relative,
        state_root,
        max_bytes=_MAX_CAPTURE_INTENT_BYTES,
        owner_only=True,
    )
    if len(payload) != row["byte_size"]:
        raise ValueError("capture intent size changed")
    if sha256_bytes(payload) != row["intent_sha256"]:
        raise ValueError("capture intent digest changed")


def _valid_capture_path(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("run/capture-intents/"):
        return False
    if "\\" in value or ".." in Path(value).parts:
        return False
    return True


def _queue_artifact_blockers(state_root: Path, deadline: float) -> set[str]:
    blockers: set[str] = set()
    for relative, code in (
        ("run/capture-intents", "capture_intent_retained"),
        ("run/queue-results", "queue_result_retained"),
        ("run/queue-quarantine", "queue_quarantine_retained"),
    ):
        if _bounded_entries(
            state_root / relative, state_root=state_root, deadline=deadline
        ):
            blockers.add(code)
    return blockers


def validate_coordinator_v3_runtime(
    *,
    state_root: Path,
    now: datetime,
    deadline: float,
    excluded_owner: OwnerLease | None,
) -> list[str]:
    """Return bounded coordinator-v3 retention and consistency blockers."""
    blockers: set[str] = set()
    try:
        state = Path(state_root)
        database_blockers, known, retained = _coordinator_database_blockers(
            state, now, deadline, excluded_owner
        )
        blockers.update(database_blockers)
        blockers.update(_transaction_artifact_blockers(state, deadline, known, retained))
    except TimeoutError:
        raise
    except (OSError, PermissionError, sqlite3.Error, ValueError):
        blockers.add("transaction_state_unreadable")
    return sorted(blockers)


def _coordinator_database_blockers(
    state_root: Path,
    now: datetime,
    deadline: float,
    excluded_owner: OwnerLease | None,
) -> tuple[set[str], set[str], set[str]]:
    path = state_root / "run" / "markdown-transactions-v3.sqlite3"
    with contextlib.closing(
        open_readonly_operational_db(
            path,
            state_root,
            max_bytes=_MAX_OPERATIONAL_DB_BYTES,
            owner_only=True,
            contract=_COORDINATOR_CONTRACT,
        )
    ) as database:
        blockers = _coordinator_owner_blockers(database, deadline, excluded_owner)
        blockers.update(_coordinator_table_blockers(database, deadline))
        transaction_blockers, known, retained = _transaction_blockers(
            database, state_root, now, deadline
        )
        blockers.update(transaction_blockers)
    return blockers, known, retained


def _coordinator_owner_blockers(
    database: sqlite3.Connection,
    deadline: float,
    excluded_owner: OwnerLease | None,
) -> set[str]:
    rows = _bounded_rows(database, "SELECT * FROM maintenance_owners", deadline=deadline)
    blockers: set[str] = set()
    for row in rows:
        if not _owner_matches(row, excluded_owner):
            blockers.add("canonical_owner_retained")
    return blockers


def _coordinator_table_blockers(
    database: sqlite3.Connection, deadline: float
) -> set[str]:
    blockers: set[str] = set()
    for table, code in _COORDINATOR_TABLE_CODES:
        rows = _bounded_rows(database, f'SELECT * FROM "{table}"', deadline=deadline)
        if rows:
            blockers.add(code)
    return blockers


def _transaction_blockers(
    database: sqlite3.Connection, state_root: Path, now: datetime, deadline: float
) -> tuple[set[str], set[str], set[str]]:
    rows = _bounded_rows(database, 'SELECT * FROM "transaction"', deadline=deadline)
    cutoff = now.astimezone(timezone.utc) - timedelta(days=_UNDO_RETENTION_DAYS)
    blockers: set[str] = set()
    known: set[str] = set()
    retained: set[str] = set()
    for row in rows:
        transaction_id, code, keep = _transaction_retention(row, state_root, cutoff)
        known.add(transaction_id)
        if code is not None:
            blockers.add(code)
        if keep:
            retained.add(transaction_id)
    return blockers, known, retained


def _transaction_retention(
    row: sqlite3.Row, state_root: Path, cutoff: datetime
) -> tuple[str, str | None, bool]:
    transaction_id = _transaction_id(row["id"])
    state = row["state"]
    code = _TRANSACTION_STATE_CODES.get(state)
    if code is not None:
        return transaction_id, code, True
    if state in {"committed", "aborted"}:
        return _terminal_transaction_retention(row, state_root, cutoff, transaction_id)
    if state == "discarded":
        return transaction_id, None, False
    raise ValueError("transaction state is unknown")


def _transaction_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("transaction ID is invalid")
    if not value:
        raise ValueError("transaction ID is invalid")
    return value


def _terminal_transaction_retention(
    row: sqlite3.Row, state_root: Path, cutoff: datetime, transaction_id: str
) -> tuple[str, str | None, bool]:
    if row["state"] == "aborted":
        _validate_abort_receipt(row, state_root, transaction_id)
    updated = _parse_timestamp(row["updated_at"])
    if updated >= cutoff and row["artifacts_pruned_at"] is None:
        return transaction_id, "transaction_undo_retained", True
    return transaction_id, None, False


def _validate_abort_receipt(
    row: sqlite3.Row, state_root: Path, transaction_id: str
) -> None:
    receipt = state_root / "run" / "transactions" / transaction_id / "abort-receipt.json"
    payload = read_runtime_bytes(
        receipt, state_root, max_bytes=_MAX_RECORD_BYTES, owner_only=True
    )
    if sha256_bytes(payload) != row["abort_receipt_sha256"]:
        raise ValueError("aborted transaction receipt changed")


def _transaction_artifact_blockers(
    state_root: Path,
    deadline: float,
    known: set[str],
    retained: set[str],
) -> set[str]:
    entries = _bounded_entries(
        state_root / "run" / "transactions", state_root=state_root, deadline=deadline
    )
    artifact_ids = {_transaction_artifact_id(entry) for entry in entries}
    blockers: set[str] = set()
    if retained & artifact_ids:
        blockers.add("transaction_artifact_retained")
    if artifact_ids - known:
        blockers.add("transaction_artifact_state_unknown")
    return blockers


def _transaction_artifact_id(entry: Path) -> str:
    if _kind(entry) != "directory":
        raise ValueError("transaction artifact is not a directory")
    if re.fullmatch(r"[0-9a-z_-]{1,128}", entry.name) is None:
        raise ValueError("transaction artifact name is invalid")
    return entry.name


def _blackboard_claim_blockers(
    *, state_root: Path, now: datetime, deadline: float
) -> list[str]:
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
            rows = _bounded_rows(
                database,
                "SELECT expires_at FROM blackboard_claims",
                deadline=deadline,
            )
            if any(_parse_timestamp(row["expires_at"]) > now for row in rows):
                blockers.add("blackboard_claim_retained")
    except TimeoutError:
        raise
    except (OSError, PermissionError, sqlite3.Error, ValueError):
        blockers.add("transaction_state_unreadable")
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
    blockers.update(
        _blackboard_claim_blockers(
            state_root=state_root,
            now=now,
            deadline=deadline,
        )
    )
    blockers.update(_install_deletion_blockers(Path(state_root)))
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
    source_state = str(migration["source_state"])
    _validate_partial_databases(paths, state_root, source_state)
    for name in operation_artifacts:
        spec = _operation_artifact_spec(name)
        spec.validate(paths["run"] / name, state_root=state_root)


def _validate_partial_databases(
    paths: dict[str, Path], state_root: Path, source_state: str
) -> None:
    for spec in _DATABASE_SPECS:
        _validate_partial_active(paths[spec.active_key], state_root, spec)
        _validate_partial_legacy(paths[spec.legacy_key], state_root, source_state)
        _validate_partial_retired(paths[spec.retired_key], state_root, source_state)


def _validate_partial_active(
    path: Path, state_root: Path, spec: _DatabaseSpec
) -> None:
    kind = _kind(path)
    if kind == "missing":
        return
    if kind != "file":
        raise ValueError("v3 active database path has the wrong kind")
    spec.validate(path, state_root=state_root)


def _validate_partial_legacy(path: Path, state_root: Path, source_state: str) -> None:
    kind = _kind(path)
    if kind == "missing":
        return
    if kind != "file":
        raise ValueError("legacy database path has the wrong kind")
    try:
        _read_record(
            path,
            state_root,
            schema=_TOMBSTONE_SCHEMA,
            max_bytes=_MAX_TOMBSTONE_BYTES,
        )
    except (PermissionError, ValueError):
        if source_state != "upgrade":
            raise
        _validate_legacy_database(path, state_root)


def _validate_partial_retired(path: Path, state_root: Path, source_state: str) -> None:
    kind = _kind(path)
    if kind == "missing":
        return
    if source_state == "fresh":
        raise ValueError("fresh migration has retired database evidence")
    if kind != "file":
        raise ValueError("retired database path has the wrong kind")
    read_runtime_bytes(
        path, state_root, max_bytes=_MAX_OPERATIONAL_DB_BYTES, owner_only=True
    )


def _operation_artifact_spec(name: str) -> _DatabaseSpec:
    for spec in _DATABASE_SPECS:
        if name.endswith(f"{spec.name}.candidate.sqlite3"):
            return spec
    raise ValueError("unknown Reliability V3 operation artifact")


def _prepare_adoption_root(state_root: Path) -> dict[str, Path]:
    validate_state_root(state_root)
    paths = _paths(state_root)
    created = _kind(paths["run"]) == "missing"
    paths["run"].mkdir(parents=True, exist_ok=True)
    memory_queue._harden_owner_only(paths["run"], 0o700)
    if created:
        sync_runtime_directory(state_root)
    return paths


def _integration_digest(root: Path) -> str:
    payload = read_stable_bytes(
        root / "scripts" / "integration_adapter.py",
        _MAX_INTEGRATION_BYTES,
        label="installed integration adapter",
    )
    return sha256_bytes(payload)


def _base_migration_descriptor(spec: _DatabaseSpec) -> dict[str, object]:
    return {
        "database": spec.name,
        "legacy_path": spec.legacy_path,
        "replacement_path": spec.active_path,
    }


def _upgrade_migration_descriptor(
    spec: _DatabaseSpec, path: Path, state_root: Path
) -> tuple[dict[str, object], bytes]:
    _preflight_upgrade_source(spec, path, state_root)
    payload = read_runtime_bytes(
        path, state_root, max_bytes=_MAX_OPERATIONAL_DB_BYTES, owner_only=True
    )
    identity = capture_runtime_file_identity(path, state_root=state_root)
    record = _base_migration_descriptor(spec)
    record.update(
        {
            "retired_path": spec.retired_path,
            "source_identity": _identity_value(identity),
            "source_sha256": sha256_bytes(payload),
        }
    )
    return record, payload


def _preflight_upgrade_source(
    spec: _DatabaseSpec, path: Path, state_root: Path
) -> None:
    _require_no_sqlite_sidecars(path)
    _validate_legacy_database(path, state_root)
    if spec.name == "queue":
        _reject_leased_v2_tasks(path, state_root)


def _require_no_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-journal", "-shm", "-wal"):
        if _kind(Path(f"{path}{suffix}")) != "missing":
            raise ValueError("legacy SQLite sidecar blocks offline adoption")


def _require_legacy_markers_quiescent(run: Path, state_root: Path) -> None:
    for name, parser in (
        ("compile.pid", _compile_marker_pid),
        ("maintenance.lock", _maintenance_marker_pid),
    ):
        pid = _read_marker_pid(run / name, state_root, parser)
        if pid is not None:
            _require_process_absent(pid)


def _read_marker_pid(
    path: Path, state_root: Path, parser: Callable[[bytes], int]
) -> int | None:
    kind = _kind(path)
    if kind == "missing":
        return None
    if kind != "file":
        raise ValueError("legacy ownership marker has the wrong kind")
    payload = read_runtime_bytes(path, state_root, max_bytes=4096, owner_only=False)
    return parser(payload)


def _compile_marker_pid(payload: bytes) -> int:
    lines = _ascii_marker_lines(payload)
    _require_compile_marker_lines(lines)
    datetime.fromisoformat(lines[1])
    return _positive_pid(lines[0])


def _require_compile_marker_lines(lines: list[str]) -> None:
    if len(lines) not in {2, 3}:
        raise ValueError("compile marker shape is invalid")
    if len(lines) == 3 and not lines[2]:
        raise ValueError("compile marker owner token is empty")


def _maintenance_marker_pid(payload: bytes) -> int:
    lines = _ascii_marker_lines(payload)
    if len(lines) != 1:
        raise ValueError("maintenance marker shape is invalid")
    return _positive_pid(lines[0])


def _ascii_marker_lines(payload: bytes) -> list[str]:
    text = payload.decode("ascii", errors="strict")
    lines = text.strip().splitlines()
    if not lines:
        raise ValueError("legacy ownership marker is empty")
    return lines


def _positive_pid(value: str) -> int:
    pid = int(value)
    if pid <= 0:
        raise ValueError("legacy ownership marker PID is invalid")
    return pid


def _require_process_absent(pid: int) -> None:
    from operational_ownership import process_start_identity

    if process_start_identity(pid) is not None:
        raise ValueError("live legacy owner blocks offline adoption")


def _reject_leased_v2_tasks(path: Path, state_root: Path) -> None:
    with contextlib.closing(
        open_readonly_operational_db(
            path,
            state_root,
            max_bytes=_MAX_OPERATIONAL_DB_BYTES,
            owner_only=True,
        )
    ) as database:
        leased = database.execute(
            "SELECT COUNT(*) FROM tasks WHERE state='leased'"
        ).fetchone()[0]
    if leased:
        raise ValueError("leased v2 queue task blocks offline adoption")


def _new_migration_record(
    *, root: Path, state_root: Path, source_state: str
) -> tuple[dict[str, object], dict[str, bytes]]:
    paths = _paths(state_root)
    databases, sources = _migration_descriptors(paths, state_root, source_state)
    body = {
        "source_state": source_state,
        "databases": databases,
        "schemas": _expected_schema_digests(),
        "installed_integration_sha256": _integration_digest(root),
    }
    operation_hash = sha256_bytes(canonical_json_bytes(body))
    migration = {
        "schema_version": "reliability-v3-migration/v1",
        "operation_id": f"reliability-v3:{operation_hash}",
        **body,
    }
    _validate_canonical_record(migration, _MIGRATION_SCHEMA, _MAX_RECORD_BYTES)
    return migration, sources


def _migration_descriptors(
    paths: dict[str, Path], state_root: Path, source_state: str
) -> tuple[list[dict[str, object]], dict[str, bytes]]:
    records: list[dict[str, object]] = []
    sources: dict[str, bytes] = {}
    for spec in _DATABASE_SPECS:
        if source_state == "upgrade":
            record, payload = _upgrade_migration_descriptor(
                spec, paths[spec.legacy_key], state_root
            )
            records.append(record)
            sources[spec.name] = payload
            continue
        records.append(_base_migration_descriptor(spec))
    return records, sources


def _validate_canonical_record(value: dict[str, object], schema: Path, limit: int) -> bytes:
    validate_schema(value, schema)
    payload = canonical_json_bytes(value)
    if len(payload) > limit:
        raise ValueError("Reliability V3 record exceeds its byte limit")
    return payload


def _publish_immutable_record(
    path: Path,
    value: dict[str, object],
    *,
    state_root: Path,
    schema: Path,
    limit: int,
) -> bytes:
    payload = _validate_canonical_record(value, schema, limit)
    publish_runtime_file(
        path, payload, state_root=state_root, create_only=True, mode=0o600
    )
    return payload


def _load_migration(paths: dict[str, Path], state_root: Path) -> dict[str, object]:
    return _read_record(
        paths["migration"],
        state_root,
        schema=_MIGRATION_SCHEMA,
        max_bytes=_MAX_RECORD_BYTES,
    )


def _validate_migration_context(
    migration: dict[str, object], root: Path
) -> dict[str, dict[str, object]]:
    if migration.get("schemas") != _expected_schema_digests():
        raise ValueError("migration schema digests changed")
    if migration.get("installed_integration_sha256") != _integration_digest(root):
        raise ValueError("installed integration changed during adoption")
    records = _named_database_records(migration.get("databases"))
    _validate_migration_database_paths(migration, records)
    _validate_migration_operation_id(migration)
    return records


def _validate_migration_database_paths(
    migration: dict[str, object], records: dict[str, dict[str, object]]
) -> None:
    source_state = str(migration["source_state"])
    for spec in _DATABASE_SPECS:
        expected = _expected_migration_record(
            database_name=spec.name,
            source_state=source_state,
            migration_record=records[spec.name],
        )
        if records[spec.name] != expected:
            raise ValueError("migration database descriptor changed")


def _validate_migration_operation_id(migration: dict[str, object]) -> None:
    body = {
        "source_state": migration.get("source_state"),
        "databases": migration.get("databases"),
        "schemas": migration.get("schemas"),
        "installed_integration_sha256": migration.get("installed_integration_sha256"),
    }
    expected = f"reliability-v3:{sha256_bytes(canonical_json_bytes(body))}"
    if migration.get("operation_id") != expected:
        raise ValueError("migration operation identity changed")


def _operation_hash(migration: dict[str, object]) -> str:
    operation_id = migration.get("operation_id")
    if not isinstance(operation_id, str):
        raise ValueError("migration operation ID is missing")
    match = re.fullmatch(r"reliability-v3:([0-9a-f]{64})", operation_id)
    if match is None:
        raise ValueError("migration operation ID is invalid")
    return match.group(1)


def _candidate_path(paths: dict[str, Path], migration: dict[str, object], spec: _DatabaseSpec) -> Path:
    return paths["run"] / f".reliability-v3-{_operation_hash(migration)}-{spec.name}.candidate.sqlite3"


def _migration_source_bytes(
    spec: _DatabaseSpec,
    paths: dict[str, Path],
    state_root: Path,
    record: dict[str, object],
) -> bytes:
    retired = paths[spec.retired_key]
    if _kind(retired) == "file":
        return _verified_source_bytes(retired, state_root, record, check_identity=False)
    if _kind(retired) != "missing":
        raise ValueError("retired database path has the wrong kind")
    return _verified_source_bytes(
        paths[spec.legacy_key], state_root, record, check_identity=True
    )


def _verified_source_bytes(
    path: Path,
    state_root: Path,
    record: dict[str, object],
    *,
    check_identity: bool,
) -> bytes:
    payload = read_runtime_bytes(
        path, state_root, max_bytes=_MAX_OPERATIONAL_DB_BYTES, owner_only=True
    )
    if sha256_bytes(payload) != record.get("source_sha256"):
        raise ValueError("migration source digest changed")
    if check_identity:
        _require_source_identity(path, state_root, record)
    return payload


def _require_source_identity(
    path: Path, state_root: Path, record: dict[str, object]
) -> None:
    identity = capture_runtime_file_identity(path, state_root=state_root)
    if _identity_value(identity) != record.get("source_identity"):
        raise ValueError("migration source identity changed")


def _ensure_retired_database(
    spec: _DatabaseSpec,
    paths: dict[str, Path],
    state_root: Path,
    record: dict[str, object],
) -> Path:
    payload = _migration_source_bytes(spec, paths, state_root, record)
    retired = paths[spec.retired_key]
    publish_runtime_file(
        retired, payload, state_root=state_root, create_only=True, mode=0o600
    )
    _verified_source_bytes(retired, state_root, record, check_identity=False)
    return retired


def _prepare_candidate(
    spec: _DatabaseSpec,
    candidate: Path,
    source: Path | None,
    state_root: Path,
) -> None:
    kind = _kind(candidate)
    if kind not in {"missing", "file"}:
        raise ValueError("Reliability V3 candidate path has the wrong kind")
    spec.initialize(candidate, source_v2=source)
    spec.validate(candidate, state_root=state_root)


def _publish_candidate(
    spec: _DatabaseSpec,
    candidate: Path,
    active: Path,
    state_root: Path,
) -> None:
    payload = read_runtime_bytes(
        candidate, state_root, max_bytes=_MAX_OPERATIONAL_DB_BYTES, owner_only=True
    )
    durable_publish_file(
        candidate,
        active,
        replace=False,
        expected_sha256=sha256_bytes(payload),
        max_bytes=_MAX_OPERATIONAL_DB_BYTES,
    )
    _remove_matching_candidate(candidate, active, state_root)
    spec.validate(active, state_root=state_root)


def _remove_matching_candidate(candidate: Path, active: Path, state_root: Path) -> None:
    if _kind(candidate) == "missing":
        return
    candidate_bytes = read_runtime_bytes(
        candidate, state_root, max_bytes=_MAX_OPERATIONAL_DB_BYTES, owner_only=True
    )
    active_bytes = read_runtime_bytes(
        active, state_root, max_bytes=_MAX_OPERATIONAL_DB_BYTES, owner_only=True
    )
    if candidate_bytes != active_bytes:
        raise ValueError("active and candidate databases conflict")
    candidate.unlink()
    sync_runtime_directory(candidate.parent)


def _matching_tombstone(
    path: Path, state_root: Path, expected: dict[str, object]
) -> bool:
    if _kind(path) != "file":
        return False
    try:
        observed = _read_record(
            path,
            state_root,
            schema=_TOMBSTONE_SCHEMA,
            max_bytes=_MAX_TOMBSTONE_BYTES,
        )
    except (PermissionError, ValueError):
        return False
    return observed == expected


def _publish_tombstone(
    *,
    path: Path,
    state_root: Path,
    source_state: str,
    expected: dict[str, object],
    migration_record: dict[str, object],
) -> None:
    payload = _validate_canonical_record(
        expected, _TOMBSTONE_SCHEMA, _MAX_TOMBSTONE_BYTES
    )
    if _matching_tombstone(path, state_root, expected):
        return
    if source_state == "fresh":
        _publish_fresh_tombstone(path, payload, state_root)
        return
    _replace_upgrade_source(path, payload, state_root, migration_record)


def _publish_fresh_tombstone(path: Path, payload: bytes, state_root: Path) -> None:
    if _kind(path) != "missing":
        raise ValueError("fresh legacy path is occupied")
    publish_runtime_file(
        path, payload, state_root=state_root, create_only=True, mode=0o600
    )


def _replace_upgrade_source(
    path: Path,
    payload: bytes,
    state_root: Path,
    migration_record: dict[str, object],
) -> None:
    if _kind(path) != "file":
        raise ValueError("upgrade source path is unavailable")
    source = _verified_source_bytes(
        path, state_root, migration_record, check_identity=True
    )
    identity = capture_runtime_file_identity(path, state_root=state_root)
    publish_runtime_file(
        path,
        payload,
        state_root=state_root,
        create_only=False,
        expected=identity,
        expected_sha256=sha256_bytes(source),
        mode=0o600,
    )


def _adopt_database(
    *,
    spec: _DatabaseSpec,
    paths: dict[str, Path],
    state_root: Path,
    migration: dict[str, object],
    migration_record: dict[str, object],
) -> None:
    source_state = str(migration["source_state"])
    source = _candidate_source(spec, paths, state_root, source_state, migration_record)
    candidate = _candidate_path(paths, migration, spec)
    active = paths[spec.active_key]
    if _kind(active) == "file":
        _resume_active_database(spec, candidate, active, state_root)
        return
    _prepare_candidate(spec, candidate, source, state_root)
    tombstone = _expected_tombstone(
        database_name=spec.name,
        source_state=source_state,
        operation_id=str(migration["operation_id"]),
        adoption_schema_sha256=migration["schemas"]["adoption_schema_sha256"],
        migration_record=migration_record,
    )
    _publish_tombstone(
        path=paths[spec.legacy_key],
        state_root=state_root,
        source_state=source_state,
        expected=tombstone,
        migration_record=migration_record,
    )
    _publish_candidate(spec, candidate, active, state_root)


def _candidate_source(
    spec: _DatabaseSpec,
    paths: dict[str, Path],
    state_root: Path,
    source_state: str,
    migration_record: dict[str, object],
) -> Path | None:
    if source_state == "fresh":
        return None
    return _ensure_retired_database(spec, paths, state_root, migration_record)


def _resume_active_database(
    spec: _DatabaseSpec, candidate: Path, active: Path, state_root: Path
) -> None:
    spec.validate(active, state_root=state_root)
    _remove_matching_candidate(candidate, active, state_root)


def _artifact_record(path: Path, state_root: Path, max_bytes: int) -> dict[str, object]:
    payload = read_runtime_bytes(
        path, state_root, max_bytes=max_bytes, owner_only=True
    )
    identity = capture_runtime_file_identity(path, state_root=state_root)
    return {
        "path": path.relative_to(state_root).as_posix(),
        "sha256": sha256_bytes(payload),
        "identity": _identity_value(identity),
    }


def _adoption_database_record(
    spec: _DatabaseSpec,
    paths: dict[str, Path],
    state_root: Path,
    source_state: str,
) -> dict[str, object]:
    validation = spec.validate(paths[spec.active_key], state_root=state_root)
    record: dict[str, object] = {
        "database": spec.name,
        "active": _artifact_record(
            paths[spec.active_key], state_root, _MAX_OPERATIONAL_DB_BYTES
        ),
        "tombstone": _artifact_record(
            paths[spec.legacy_key], state_root, _MAX_TOMBSTONE_BYTES
        ),
        "application_id": validation["application_id"],
        "user_version": validation["user_version"],
        "pragmas": {
            "journal_mode": validation["journal_mode"],
            "synchronous": validation["synchronous"],
            "foreign_keys": 1,
            "trusted_schema": validation["trusted_schema"],
        },
    }
    if source_state == "upgrade":
        record["retired"] = _artifact_record(
            paths[spec.retired_key], state_root, _MAX_OPERATIONAL_DB_BYTES
        )
    return record


def _build_adoption_record(
    migration: dict[str, object], paths: dict[str, Path], state_root: Path
) -> dict[str, object]:
    migration_bytes = read_runtime_bytes(
        paths["migration"], state_root, max_bytes=_MAX_RECORD_BYTES, owner_only=True
    )
    source_state = str(migration["source_state"])
    return {
        "schema_version": "reliability-v3-adoption/v1",
        "operation_id": migration["operation_id"],
        "source_state": source_state,
        "migration": {
            "path": "run/reliability-v3-migration.json",
            "sha256": sha256_bytes(migration_bytes),
        },
        "databases": [
            _adoption_database_record(spec, paths, state_root, source_state)
            for spec in _DATABASE_SPECS
        ],
        "schemas": migration["schemas"],
        "installed_integration_sha256": migration["installed_integration_sha256"],
    }


def _complete_adoption(
    *, root: Path, state_root: Path, migration: dict[str, object]
) -> dict[str, object]:
    paths = _paths(state_root)
    records = _validate_migration_context(migration, root)
    for spec in _DATABASE_SPECS:
        _adopt_database(
            spec=spec,
            paths=paths,
            state_root=state_root,
            migration=migration,
            migration_record=records[spec.name],
        )
    adoption = _build_adoption_record(migration, paths, state_root)
    _publish_immutable_record(
        paths["adoption"],
        adoption,
        state_root=state_root,
        schema=_ADOPTION_SCHEMA,
        limit=_MAX_RECORD_BYTES,
    )
    return require_reliability_v3_adopted(root=root, state_root=state_root)


def _report(
    *,
    mode: Literal["check", "apply"],
    status: Literal["ok", "degraded", "error"],
    state: str,
    blockers: list[str],
    artifacts: dict[str, object] | None = None,
    actions: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    details: dict[str, object] = {"adoption_state": state}
    if artifacts is not None:
        details["artifacts"] = artifacts
    if actions is None:
        actions = []
    return {
        "mode": mode,
        "overall_status": status,
        "actions": actions,
        "blockers": [{"code": code} for code in blockers],
        "details": details,
    }


def inspect_installed_vault(*, root: Path, state_root: Path) -> dict[str, object]:
    """Run bounded Reliability V3 validation without creating or mutating state."""
    try:
        return _inspect_installed_vault(root=Path(root), state_root=Path(state_root))
    except Exception:  # noqa: BLE001 - closed read-only inspection envelope
        return _report(
            mode="check",
            status="error",
            state="conflict",
            blockers=["reliability_v3_record_invalid"],
        )


def _inspect_installed_vault(*, root: Path, state_root: Path) -> dict[str, object]:
    vault = root.resolve(strict=True)
    if not vault.is_dir():
        raise ValueError("vault root is not a directory")
    state = state_root.absolute()
    paths = _paths(state)
    kinds = _present_path_kinds(paths)
    operation_artifacts, overflow = _operation_artifacts(paths["run"])
    if not _v3_evidence_present(kinds, operation_artifacts, overflow):
        return _inspect_legacy_pair(paths, state, kinds)
    return _inspect_v3_state(
        vault, state, paths, kinds, operation_artifacts, overflow
    )


def _present_path_kinds(paths: dict[str, Path]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, path in paths.items():
        kind = _kind(path)
        if name != "run" and kind != "missing":
            result[name] = kind
    return result


def _v3_evidence_present(
    kinds: dict[str, str], operation_artifacts: list[str], overflow: bool
) -> bool:
    names = {
        "queue_active",
        "coordinator_active",
        "queue_retired",
        "coordinator_retired",
        "migration",
        "adoption",
    }
    return bool(names & set(kinds)) or bool(operation_artifacts) or overflow


def _inspect_legacy_pair(
    paths: dict[str, Path], state: Path, kinds: dict[str, str]
) -> dict[str, object]:
    pair = (_kind(paths["queue_legacy"]), _kind(paths["coordinator_legacy"]))
    if pair == ("missing", "missing"):
        return _inspect_fresh(paths)
    if pair == ("file", "file"):
        _require_legacy_markers_quiescent(paths["run"], state)
        _require_no_sqlite_sidecars(paths["queue_legacy"])
        _require_no_sqlite_sidecars(paths["coordinator_legacy"])
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


def _inspect_fresh(paths: dict[str, Path]) -> dict[str, object]:
    legacy_evidence = _legacy_evidence(paths["run"])
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


def _inspect_v3_state(
    vault: Path,
    state: Path,
    paths: dict[str, Path],
    kinds: dict[str, str],
    operation_artifacts: list[str],
    overflow: bool,
) -> dict[str, object]:
    artifacts = {"paths": kinds, "operation_artifacts": operation_artifacts}
    if overflow:
        return _report(
            mode="check",
            status="error",
            state="conflict",
            blockers=["reliability_v3_operation_artifact_limit_exceeded"],
            artifacts=artifacts,
        )
    if _kind(paths["migration"]) != "file":
        return _report(
            mode="check",
            status="error",
            state="conflict",
            blockers=["reliability_v3_migration_record_missing"],
            artifacts=artifacts,
        )
    migration = _read_record(
        paths["migration"], state, schema=_MIGRATION_SCHEMA, max_bytes=_MAX_RECORD_BYTES
    )
    _validate_partial_artifacts(paths, state, migration, operation_artifacts)
    if _kind(paths["adoption"]) == "missing":
        return _report(
            mode="check",
            status="degraded",
            state="partial",
            blockers=["reliability_v3_runtime_activation_incomplete"],
            artifacts=artifacts,
        )
    return _inspect_adopted(vault, state, paths, migration, operation_artifacts, artifacts)


def _inspect_adopted(
    vault: Path,
    state: Path,
    paths: dict[str, Path],
    migration: dict[str, object],
    operation_artifacts: list[str],
    artifacts: dict[str, object],
) -> dict[str, object]:
    if _kind(paths["adoption"]) != "file":
        raise ValueError("adoption record path has the wrong kind")
    adoption = _read_record(
        paths["adoption"], state, schema=_ADOPTION_SCHEMA, max_bytes=_MAX_RECORD_BYTES
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
        mode="check", status="ok", state="adopted", blockers=[], artifacts=artifacts
    )


def repair_installed_vault(
    *,
    root: Path,
    state_root: Path,
    adopt_ownership_v3: bool,
    confirm_all_agents_stopped: bool,
) -> dict[str, object]:
    """Perform the explicit resumable offline Reliability V3 adoption."""
    if not adopt_ownership_v3 or not confirm_all_agents_stopped:
        return _report(
            mode="apply",
            status="error",
            state="conflict",
            blockers=["reliability_v3_offline_confirmation_required"],
        )
    try:
        changed = _apply_reliability_v3_adoption(
            root=Path(root), state_root=Path(state_root)
        )
    except Exception:  # noqa: BLE001 - stable redacted repair boundary
        return _report(
            mode="apply",
            status="error",
            state="conflict",
            blockers=["reliability_v3_adoption_failed"],
        )
    actions = [{"code": "reliability_v3_adopted"}] if changed else []
    return _report(
        mode="apply", status="ok", state="adopted", blockers=[], actions=actions
    )


def _apply_reliability_v3_adoption(*, root: Path, state_root: Path) -> bool:
    inspection = inspect_installed_vault(root=root, state_root=state_root)
    status = inspection.get("overall_status")
    state = inspection.get("details", {}).get("adoption_state")
    if status == "error":
        raise ValueError("Reliability V3 inspection failed")
    if state == "adopted":
        return False
    if state not in {"fresh", "upgrade-required", "partial"}:
        raise ValueError("Reliability V3 state cannot be adopted")
    paths = _prepare_adoption_root(state_root)
    migration = _migration_for_apply(root, state_root, paths, str(state))
    _complete_adoption(root=root, state_root=state_root, migration=migration)
    return True


def _migration_for_apply(
    root: Path, state_root: Path, paths: dict[str, Path], inspected_state: str
) -> dict[str, object]:
    if _kind(paths["migration"]) == "file":
        return _load_migration(paths, state_root)
    source_state = "upgrade" if inspected_state == "upgrade-required" else "fresh"
    migration, _sources = _new_migration_record(
        root=root, state_root=state_root, source_state=source_state
    )
    _publish_immutable_record(
        paths["migration"],
        migration,
        state_root=state_root,
        schema=_MIGRATION_SCHEMA,
        limit=_MAX_RECORD_BYTES,
    )
    return migration
