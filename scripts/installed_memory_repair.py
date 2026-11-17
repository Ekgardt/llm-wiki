"""Read-only inspection for the deferred Reliability V3 runtime adoption."""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple

import markdown_transaction
import queue_core
from bounded_io import read_stable_bytes
from llm_wiki import sqlite_io
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.formats.schema import validate_schema
from llm_wiki.sqlite_deadlines import (
    busy_database,
)
from reliable_memory import (
    capture_runtime_file_identity,
    durable_publish_file,
    publish_runtime_file,
    read_runtime_bytes,
    sync_runtime_directory,
    validate_state_root,
)
from secret_redact import describe_error_chain

if TYPE_CHECKING:
    pass

from llm_wiki import adoption_admission
from llm_wiki.adoption_admission import (  # noqa: E402
    MAX_OPERATIONAL_DB_BYTES,
    MAX_RECORD_BYTES,
    MAX_TOMBSTONE_BYTES,
    MIGRATION_SCHEMA,
    STRAY_SPECS,
    TOMBSTONE_SCHEMA,
    adoption_paths,
    adoption_schema,
    current_schema_digests,
    expected_migration_record,
    expected_tombstone,
    identity_value,
    listed_operation_artifacts,
    named_database_records,
    read_adoption_record,
    read_record,
    require_reliability_v3_adopted,
    validate_complete_adoption,
    validated_schema_digests,
)
from llm_wiki.adoption_admission import (
    kind as _kind,
)
from reliability_v3_validation import (
    artifact_record,
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
# Bounded read of scripts/integration_adapter.py for its digest: the file is
# 133 040 bytes on 2026-09-27, so 16 MiB only refuses something that is not the
# adapter. A guard, not a tunable.
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
        queue_core.initialize_queue_v3_candidate,
        queue_core.validate_queue_v3_database,
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
    if kind == "missing":
        return None
    if kind == "directory":
        return f"run/{name}" if _nonempty_directory(run / name) else None
    return f"run/{name}" if kind == "file" else f"run/{name}:{kind}"



def _current_initialization(record):
    return record.get("schemas") == current_schema_digests()


def _initial_adoption_version(migration):
    if _current_initialization(migration):
        return "reliability-v3-adoption/v3"
    return "reliability-v3-adoption/v1"


QUARANTINE_DIRECTORIES = tuple(spec.quarantine for spec in STRAY_SPECS)


def _read_legacy_schema(path: Path) -> None:
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


def _validate_legacy_database(path: Path, state_root: Path) -> None:
    """Read a legacy database's schema, surviving a crashed writer's journal.

    This open is deliberately minimal — no contract, no owner check — so it
    cannot go through `open_readonly_operational_db`, which is where the
    stranded-journal recovery already lives. Without the same recovery here a
    legacy database left with a rollback journal fails the adoption gate with
    `reliability_v3_record_invalid`, which names neither the file nor the
    cause and fences every write in the vault. That happened twice on the live
    vault on 2026-08-29 through the shared opener; this is the same class,
    reached by the six callers of this function on the upgrade path.

    The recovery is imported under its private name on purpose. Exporting it
    means editing `reliable_memory.py`, which both complexity gates already
    refuse wholesale over 25 pre-existing findings, and every command after
    such an edit repeats that whole refusal. One first-party module borrowing
    another's helper is a smaller debt than that, and it is named here rather
    than hidden.
    """
    from reliable_memory import require_replayed_journal

    try:
        _read_legacy_schema(path)
    except sqlite3.OperationalError as error:
        require_replayed_journal(Path(path), error)
        _read_legacy_schema(path)
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
    _validate_legacy_record(path, state_root, source_state)


def _validate_legacy_record(path: Path, state_root: Path, source_state: str) -> None:
    """A tombstone, or on an upgrade the legacy database itself."""
    try:
        read_record(
            path,
            state_root,
            schema=TOMBSTONE_SCHEMA,
            max_bytes=MAX_TOMBSTONE_BYTES,
        )
    except (PermissionError, ValueError):
        if source_state != "upgrade":
            raise
        _validate_legacy_database(path, state_root)


def _validate_partial_retired(path: Path, state_root: Path, source_state: str) -> None:
    kind = _kind(path)
    if kind == "missing":
        return
    _require_retired_evidence_kind(kind, source_state)
    read_runtime_bytes(
        path, state_root, max_bytes=MAX_OPERATIONAL_DB_BYTES, owner_only=True
    )


def _require_retired_evidence_kind(kind: str, source_state: str) -> None:
    if source_state == "fresh":
        raise ValueError("fresh migration has retired database evidence")
    if kind != "file":
        raise ValueError("retired database path has the wrong kind")


def _operation_artifact_spec(name: str) -> _DatabaseSpec:
    for spec in _DATABASE_SPECS:
        if name.endswith(f"{spec.name}.candidate.sqlite3"):
            return spec
    raise ValueError("unknown Reliability V3 operation artifact")


def _prepare_adoption_root(state_root: Path) -> dict[str, Path]:
    validate_state_root(state_root)
    paths = adoption_paths(state_root)
    created = _kind(paths["run"]) == "missing"
    paths["run"].mkdir(parents=True, exist_ok=True)
    queue_core.harden_owner_only(paths["run"], 0o700)
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
        path, state_root, max_bytes=MAX_OPERATIONAL_DB_BYTES, owner_only=True
    )
    identity = capture_runtime_file_identity(path, state_root=state_root)
    record = _base_migration_descriptor(spec)
    record.update(
        {
            "retired_path": spec.retired_path,
            "source_identity": identity_value(identity),
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
        ("compile.pid", _compile_marker_owner),
        ("maintenance.lock", _maintenance_marker_owner),
    ):
        owner = _read_marker_owner(run / name, state_root, parser)
        if owner is not None:
            require_process_absent(*owner)


def _read_marker_owner(
    path: Path, state_root: Path, parser: Callable[[bytes], tuple[int, str]]
) -> tuple[int, str] | None:
    kind = _kind(path)
    if kind == "missing":
        return None
    if kind != "file":
        raise ValueError("legacy ownership marker has the wrong kind")
    payload = read_runtime_bytes(path, state_root, max_bytes=4096, owner_only=False)
    return parser(payload)


def _compile_marker_owner(payload: bytes) -> tuple[int, str]:
    lines = _ascii_marker_lines(payload)
    _require_compile_marker_lines(lines)
    datetime.fromisoformat(lines[1])
    return _positive_pid(lines[0]), (lines[3:] or [""])[0]


def _require_compile_marker_lines(lines: list[str]) -> None:
    """2 or 3 lines before 2026-09-17; a fourth names the owner's process."""
    if len(lines) not in {2, 3, 4}:
        raise ValueError("compile marker shape is invalid")
    if len(lines) >= 3 and not lines[2]:
        raise ValueError("compile marker owner token is empty")


def _maintenance_marker_owner(payload: bytes) -> tuple[int, str]:
    """One line before 2026-09-17; a second names the owner's process.

    Research: docs/research/2026-09-17-a-lock-names-the-process-not-only-its-number.md
    """
    lines = _ascii_marker_lines(payload)
    if len(lines) not in {1, 2}:
        raise ValueError("maintenance marker shape is invalid")
    return _positive_pid(lines[0]), (lines[1:] or [""])[0]


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


def require_process_absent(pid: int, identity: str) -> None:
    """A marker's PID given to another process since is not its owner (C-12)."""
    from process_liveness import recorded_process_state

    state = recorded_process_state(pid, identity)
    if state != "dead":
        label = {"alive": "live", "unknown": "unknown"}[state]
        raise ValueError(f"{label} legacy owner blocks offline adoption")


def _reject_leased_v2_tasks(path: Path, state_root: Path) -> None:
    with contextlib.closing(
        sqlite_io.open_readonly_operational_db(
            path,
            state_root,
            max_bytes=MAX_OPERATIONAL_DB_BYTES,
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
    paths = adoption_paths(state_root)
    databases, sources = _migration_descriptors(paths, state_root, source_state)
    body = {
        "source_state": source_state,
        "databases": databases,
        "schemas": adoption_admission.initial_schema_digests(source_state),
        "installed_integration_sha256": _integration_digest(root),
    }
    operation_hash = sha256_bytes(canonical_json_bytes(body))
    migration = {
        "schema_version": "reliability-v3-migration/v1",
        "operation_id": f"reliability-v3:{operation_hash}",
        **body,
    }
    _validate_canonical_record(migration, MIGRATION_SCHEMA, MAX_RECORD_BYTES)
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
    return read_record(
        paths["migration"],
        state_root,
        schema=MIGRATION_SCHEMA,
        max_bytes=MAX_RECORD_BYTES,
    )


def _validate_migration_context(
    migration: dict[str, object], root: Path
) -> dict[str, dict[str, object]]:
    validated_schema_digests(migration)
    if migration.get("installed_integration_sha256") != _integration_digest(root):
        raise ValueError("installed integration changed during adoption")
    records = named_database_records(migration.get("databases"))
    _validate_migration_database_paths(migration, records)
    _validate_migration_operation_id(migration)
    return records


def _validate_migration_database_paths(
    migration: dict[str, object], records: dict[str, dict[str, object]]
) -> None:
    source_state = str(migration["source_state"])
    for spec in _DATABASE_SPECS:
        expected = expected_migration_record(
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
        path, state_root, max_bytes=MAX_OPERATIONAL_DB_BYTES, owner_only=True
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
    if identity_value(identity) != record.get("source_identity"):
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


def prepare_candidate(
    spec: _DatabaseSpec,
    candidate: Path,
    source: Path | None,
    state_root: Path,
    migration: dict[str, object],
) -> None:
    kind = _kind(candidate)
    if kind not in {"missing", "file"}:
        raise ValueError("Reliability V3 candidate path has the wrong kind")
    _initialize_adoption_candidate(spec, candidate, source, state_root, migration)
    spec.validate(candidate, state_root=state_root)


def _initialize_adoption_candidate(spec, candidate, source, state_root, migration):
    if spec.name == "queue" and _current_initialization(migration):
        from compile_progress_migration import initialize_fresh_candidate

        initialize_fresh_candidate(candidate, state_root=state_root)
        return
    spec.initialize(candidate, source_v2=source)


def _publish_candidate(
    spec: _DatabaseSpec,
    candidate: Path,
    active: Path,
    state_root: Path,
) -> None:
    payload = read_runtime_bytes(
        candidate, state_root, max_bytes=MAX_OPERATIONAL_DB_BYTES, owner_only=True
    )
    durable_publish_file(
        candidate,
        active,
        replace=False,
        expected_sha256=sha256_bytes(payload),
        max_bytes=MAX_OPERATIONAL_DB_BYTES,
    )
    _remove_matching_candidate(candidate, active, state_root)
    spec.validate(active, state_root=state_root)


def _remove_matching_candidate(candidate: Path, active: Path, state_root: Path) -> None:
    if _kind(candidate) == "missing":
        return
    candidate_bytes = read_runtime_bytes(
        candidate, state_root, max_bytes=MAX_OPERATIONAL_DB_BYTES, owner_only=True
    )
    active_bytes = read_runtime_bytes(
        active, state_root, max_bytes=MAX_OPERATIONAL_DB_BYTES, owner_only=True
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
        observed = read_record(
            path,
            state_root,
            schema=TOMBSTONE_SCHEMA,
            max_bytes=MAX_TOMBSTONE_BYTES,
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
        expected, TOMBSTONE_SCHEMA, MAX_TOMBSTONE_BYTES
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
    prepare_candidate(spec, candidate, source, state_root, migration)
    tombstone = expected_tombstone(
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


def _adoption_database_record(
    spec: _DatabaseSpec,
    paths: dict[str, Path],
    state_root: Path,
    source_state: str,
) -> dict[str, object]:
    validation = spec.validate(paths[spec.active_key], state_root=state_root)
    record: dict[str, object] = {
        "database": spec.name,
        "active": artifact_record(
            paths[spec.active_key], state_root, None
        ),
        "tombstone": artifact_record(
            paths[spec.legacy_key], state_root, MAX_TOMBSTONE_BYTES
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
        record["retired"] = artifact_record(
            paths[spec.retired_key], state_root, None
        )
    return record


def _build_adoption_record(
    migration: dict[str, object], paths: dict[str, Path], state_root: Path
) -> dict[str, object]:
    migration_bytes = read_runtime_bytes(
        paths["migration"], state_root, max_bytes=MAX_RECORD_BYTES, owner_only=True
    )
    source_state = str(migration["source_state"])
    return {
        "schema_version": _initial_adoption_version(migration),
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
    paths = adoption_paths(state_root)
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
        schema=adoption_schema(adoption),
        limit=MAX_RECORD_BYTES,
    )
    return require_reliability_v3_adopted(root=root, state_root=state_root)


_STABLE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _stable_codes(error: BaseException) -> tuple[str, ...]:
    """The failure's own code, when it carries one in the product's own shape.

    Only the code travels: it is a fixed identifier, never a message, so the
    redaction boundary of this report is unchanged.
    """
    code = getattr(error, "code", None)
    if not isinstance(code, str) or _STABLE_CODE.fullmatch(code) is None:
        return ()
    return (code,)


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


# Failures that say the state could not be read at this moment, not that a record is
# wrong: a live writer's lock, an interrupted read. On 2026-09-28 one of them during an
# install was reported as `conflict` with no cause, and the installer told the owner
# capture was off while a second read found the vault adopted. SQLite says busy or
# locked by its result code (`SQLITE_BUSY` 5, `SQLITE_LOCKED` 6; the primary code is
# the low byte of an extended one).
_UNREADABLE_NOW = (TimeoutError, BlockingIOError, InterruptedError)
# Absolute paths in a cause name private directories; the report is printed and
# pasted, so they are replaced, and the class and the rest of the message stay.
# A quoted path (as `OSError` prints one) is taken whole, spaces included.
_QUOTED_PATH = re.compile(r"'(?:[A-Za-z]:[\\/]|/)[^']*'")
_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|(?<![\w.])/)[^\s'\"]+")
# How much of the cause a report carries: one readable line of `Class: message <- Cause`,
# redacted; the full chain of two causes fits well inside it.
MAX_INSPECTION_ERROR_CHARS = 600


def inspect_installed_vault(*, root: Path, state_root: Path) -> dict[str, object]:
    """Run bounded Reliability V3 validation without creating or mutating state.

    A failure is reported with its cause, and a state that could not be read now
    (`unreadable`) is told apart from records that disagree (`conflict`).
    See docs/research/2026-09-28-a-check-names-its-cause.md.
    """
    try:
        return _inspect_installed_vault(root=Path(root), state_root=Path(state_root))
    except Exception as exc:  # noqa: BLE001 - read-only inspection reports its cause
        return _failed_inspection(exc)


def describe_failure(exc: BaseException) -> str:
    """The redacted cause chain without absolute paths, bounded for a report line."""
    quoted = _QUOTED_PATH.sub("'<path>'", describe_error_chain(exc))
    described = _ABSOLUTE_PATH.sub("<path>", quoted)
    return described[:MAX_INSPECTION_ERROR_CHARS]


def _unreadable_now(exc: Exception) -> bool:
    """Busy or locked now, as opposed to a record that is wrong."""
    if isinstance(exc, _UNREADABLE_NOW):
        return True
    return busy_database(exc)


def _failed_inspection(exc: Exception) -> dict[str, object]:
    unreadable = _unreadable_now(exc)
    report = _report(
        mode="check",
        status="error",
        state="unreadable" if unreadable else "conflict",
        blockers=["reliability_v3_state_unreadable" if unreadable else "reliability_v3_record_invalid"],
    )
    report["details"]["error"] = describe_failure(exc)
    return report


def _inspect_installed_vault(*, root: Path, state_root: Path) -> dict[str, object]:
    vault = root.resolve(strict=True)
    if not vault.is_dir():
        raise ValueError("vault root is not a directory")
    state = state_root.absolute()
    paths = adoption_paths(state)
    kinds = _present_path_kinds(paths)
    operation_artifacts, overflow = listed_operation_artifacts(paths["run"])
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
    conflict = _v3_conflict_report(paths, overflow, artifacts)
    if conflict is not None:
        return conflict
    migration = read_record(
        paths["migration"], state, schema=MIGRATION_SCHEMA, max_bytes=MAX_RECORD_BYTES
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


def _v3_conflict_report(
    paths: dict[str, Path], overflow: bool, artifacts: dict[str, object]
) -> dict[str, object] | None:
    """The conflict report for a run directory that cannot be inspected, or None."""
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
    return None


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
    adoption = read_adoption_record(paths["adoption"], state)
    validate_complete_adoption(
        root=vault,
        state_root=state,
        paths=paths,
        migration=migration,
        adoption=adoption,
        operation_artifacts=operation_artifacts,
    )
    report = _report(
        mode="check", status="ok", state="adopted", blockers=[], artifacts=artifacts
    )
    report["details"]["compile_progress"] = {
        "reliability-v3-adoption/v1": "upgrade-required",
        "reliability-v3-adoption/v2": "upgraded",
        "reliability-v3-adoption/v3": "upgraded",
    }[adoption["schema_version"]]
    return report


def repair_installed_vault(
    *,
    root: Path,
    state_root: Path,
    adopt_ownership_v3: bool,
    confirm_all_agents_stopped: bool,
) -> dict[str, object]:
    """Perform the explicit resumable offline Reliability V3 adoption.

    A refusal names its own stable code beside the generic one, so the operator
    is told which precondition stopped the adoption. See `docs/research/
    2026-09-18-an-expired-fence-is-not-an-obstacle-to-adoption.md`.
    """
    if not adopt_ownership_v3 or not confirm_all_agents_stopped:
        return _report(
            mode="apply",
            status="error",
            state="conflict",
            blockers=["reliability_v3_offline_confirmation_required"],
        )
    try:
        changed = apply_reliability_v3_adoption(
            root=Path(root), state_root=Path(state_root)
        )
    except Exception as error:  # noqa: BLE001 - stable redacted repair boundary
        return _report(
            mode="apply",
            status="error",
            state="conflict",
            blockers=["reliability_v3_adoption_failed", *_stable_codes(error)],
        )
    actions = [{"code": "reliability_v3_adopted"}] if changed else []
    return _report(
        mode="apply", status="ok", state="adopted", blockers=[], actions=actions
    )


def _inspection_failure(inspection: dict[str, object]) -> str:
    """The failed inspection's blockers and cause, so the adoption refusal says why."""
    details = inspection.get("details", {})
    codes = ", ".join(str(item.get("code")) for item in inspection.get("blockers", []))
    cause = details.get("error") if isinstance(details, dict) else None
    return f"Reliability V3 inspection failed: {codes}" + (f" ({cause})" if cause else "")


def apply_reliability_v3_adoption(*, root: Path, state_root: Path) -> bool:
    inspection = inspect_installed_vault(root=root, state_root=state_root)
    status = inspection.get("overall_status")
    state = inspection.get("details", {}).get("adoption_state")
    if status == "error":
        raise ValueError(_inspection_failure(inspection))
    if state == "adopted":
        return False
    _require_adoptable_state(state)
    paths = _prepare_adoption_root(state_root)
    migration = _migration_for_apply(root, state_root, paths, str(state))
    _complete_adoption(root=root, state_root=state_root, migration=migration)
    return True


def _require_adoptable_state(state: object) -> None:
    if state not in {"fresh", "upgrade-required", "partial"}:
        raise ValueError("Reliability V3 state cannot be adopted")


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
        schema=MIGRATION_SCHEMA,
        limit=MAX_RECORD_BYTES,
    )
    return migration
