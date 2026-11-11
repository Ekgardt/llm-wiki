"""Whether this vault may be written: its Reliability v3 adoption is complete and intact."""
from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import NamedTuple

from llm_wiki import sqlite_io
from llm_wiki.coordinator_invariants import require_v3_invariants
from llm_wiki.coordinator_schema import (
    COORDINATOR_V3_SCHEMA_SHA256,
    coordinator_v3_schema_complete,
)
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.formats.schema import validate_schema
from llm_wiki.queue_database_contract import (
    QUEUE_COMPILE_CONTRACT,
    QUEUE_COMPILE_SCHEMA_SHA256,
    QUEUE_V3_SCHEMA_SHA256,
)
from llm_wiki.queue_schema_operations import queue_v3_schema_complete
from llm_wiki.queue_v3_readers import audit_compile_progress, open_queue_v3_readonly
from llm_wiki.reliability_defaults import DEFAULTS
from llm_wiki.sqlite_contracts import OperationalDatabaseContract
from llm_wiki.sqlite_deadlines import operational_deadline_active
from reliable_memory import (
    capture_operational_database_identity,
    capture_runtime_file_identity,
    read_runtime_bytes,
)

_SCHEMA_DIR = Path(__file__).parents[1] / "schemas"


TOMBSTONE_SCHEMA = _SCHEMA_DIR / "operational-db-tombstone-v1.json"


MIGRATION_SCHEMA = _SCHEMA_DIR / "reliability-v3-migration-v1.json"


ADOPTION_SCHEMA = _SCHEMA_DIR / "reliability-v3-adoption-v1.json"


_UPGRADED_ADOPTION_SCHEMA = _SCHEMA_DIR / "reliability-v3-adoption-v2.json"


_FRESH_ADOPTION_SCHEMA = _SCHEMA_DIR / "reliability-v3-adoption-v3.json"


# Bounded reads of the adoption's own JSON records; each is refused past its bound,
# never cut. Measured 2026-09-27 on the installed vault: a tombstone is 487-541
# bytes, the migration record 1 333 and the adoption record 2 507, so 4 KiB and
# 64 KiB leave about 8 and 25 times room. Review if a schema version adds fields.
MAX_TOMBSTONE_BYTES = 4 * 1024
MAX_RECORD_BYTES = 64 * 1024


# In-flight adoption artifacts named in a report; one adoption leaves at most one
# candidate and one retired file per database (four), so 32 means a broken run
# directory. Past it the listing sets its overflow flag and the check refuses.
_MAX_OPERATION_ARTIFACTS = 32


# Whole-file artifact reads remain byte-bounded; mutable adopted SQLite
# admission instead validates identity/schema through page-based reads.
# Runtime entry traversal keeps the same bound as doctor.MAX_RUNTIME_ENTRIES.
MAX_OPERATIONAL_DB_BYTES = 256 * 1024 * 1024


_QUEUE_CONTRACT = OperationalDatabaseContract(application_id=0x4C575133)


COORDINATOR_CONTRACT = OperationalDatabaseContract(application_id=0x4C575433)


_OPERATION_ARTIFACT_RE = re.compile(
    r"^\.reliability-v3-[0-9a-f]{64}-(?:queue|coordinator)"
    r"\.(?:candidate\.sqlite3|retired\.tmp)$"
)


def adoption_paths(state_root: Path) -> dict[str, Path]:
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


_STAT_KINDS = (
    (stat.S_ISLNK, "link"),
    (stat.S_ISREG, "file"),
    (stat.S_ISDIR, "directory"),
)


def kind(path: Path) -> str:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "missing"
    for predicate, name in _STAT_KINDS:
        if predicate(metadata.st_mode):
            return name
    return "other"


def listed_operation_artifacts(run: Path, *, deadline: float | None = None) -> tuple[list[str], bool]:
    if kind(run) != "directory":
        return [], False
    operational_deadline_active(deadline)
    names = _operation_artifact_names(run, deadline)
    names.sort()
    return names[:_MAX_OPERATION_ARTIFACTS], len(names) > _MAX_OPERATION_ARTIFACTS


def _operation_artifact_names(run: Path, deadline: float | None) -> list[str]:
    with os.scandir(run) as entries:
        return [
            entry.name
            for entry in entries
            if _is_operation_artifact_before_deadline(entry.name, deadline)
        ]


def _is_operation_artifact_before_deadline(name: str, deadline: float | None) -> bool:
    operational_deadline_active(deadline)
    return _OPERATION_ARTIFACT_RE.fullmatch(name) is not None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def read_record(
    path: Path,
    state_root: Path,
    *,
    schema: Path,
    max_bytes: int,
) -> dict[str, object]:
    value = _read_record_value(path, state_root, max_bytes=max_bytes)
    validate_schema(value, schema)
    return value


def _read_record_value(path: Path, state_root: Path, *, max_bytes: int) -> dict[str, object]:
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
    return value


def _bounded_adoption_component(value: dict) -> None:
    if len(canonical_json_bytes(value)) > MAX_RECORD_BYTES:
        raise ValueError("adoption component exceeds its metadata byte budget")


def _validate_previous_adoption(previous: dict) -> None:
    _bounded_adoption_component(previous)
    validate_schema(previous, ADOPTION_SCHEMA)


def validate_upgrade_envelope(value: dict) -> None:
    validate_schema(value, _UPGRADED_ADOPTION_SCHEMA)
    _validate_previous_adoption(value["previous"])
    _bounded_adoption_component({key: item for key, item in value.items() if key != "previous"})
    if sha256_bytes(canonical_json_bytes(value["previous"])) != value["previous_sha256"]:
        raise ValueError("previous adoption digest changed")
    if value["queue_schema_sha256"] != QUEUE_COMPILE_SCHEMA_SHA256:
        raise ValueError("compile queue schema digest changed")


def read_adoption_record(path: Path, state_root: Path) -> dict[str, object]:
    # One existing bounded record plus one bounded upgrade envelope, not DB bytes.
    value = _read_record_value(path, state_root, max_bytes=2 * MAX_RECORD_BYTES)
    if value.get("schema_version") == "reliability-v3-adoption/v2":
        validate_upgrade_envelope(value)
        return value
    _validate_direct_adoption(value)
    return value


class ReliabilityV3ValidationError(RuntimeError):
    """Stable closed failure from Reliability V3 read-only validation.

    `detail` is what the validation saw, for the operator; `code` stays the
    stable name callers match on (2026-09-23, an invalid record read the same
    as a database held by a hook for a moment).
    """

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def identity_value(identity: object) -> dict[str, object]:
    return {
        "platform": getattr(identity, "platform"),
        "volume": getattr(identity, "volume"),
        "file_id": getattr(identity, "file_id"),
        "size": getattr(identity, "size"),
        "mtime_ns": getattr(identity, "mtime_ns"),
    }


def validate_artifact_reference(
    record: object,
    *,
    expected_path: Path,
    state_root: Path,
    mutable: bool,
    max_bytes: int | None,
) -> bytes | None:
    artifact = _require_artifact_record(record, expected_path, state_root)
    identity_reader = capture_operational_database_identity if mutable else capture_runtime_file_identity
    actual_identity = identity_reader(expected_path, state_root=state_root)
    validate_artifact_identity(artifact, actual_identity, mutable)
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


def validate_artifact_identity(
    record: dict[str, object], actual_identity: object, mutable: bool
) -> None:
    expected_identity = record.get("identity")
    if not isinstance(expected_identity, dict):
        raise ValueError("artifact identity is missing")
    actual_value = identity_value(actual_identity)
    identity_fields = _identity_fields(actual_value, mutable)
    if any(expected_identity.get(field) != actual_value[field] for field in identity_fields):
        raise ValueError("artifact identity changed")


def _identity_fields(actual_value: dict[str, object], mutable: bool) -> tuple[str, ...]:
    """A mutable artifact is identified by its file, not by its contents."""
    return ("platform", "volume", "file_id") if mutable else tuple(actual_value)


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


def open_adopted_database(database_name, path, state_root, *, deadline=None):
    if database_name == "queue":
        return open_queue_v3_readonly(path, state_root, deadline=deadline)
    return sqlite_io.open_readonly_operational_db(
        path, state_root, max_bytes=None, owner_only=True,
        contract=_database_contract(database_name),
        busy_ms=DEFAULTS.markdown_busy_ms, deadline=deadline,
    )


@contextlib.contextmanager
def _active_database_snapshot(record: dict[str, object], *, database_name: str, path: Path, state_root: Path, deadline: float | None = None) -> Iterator[sqlite3.Connection]:
    operational_deadline_active(deadline)
    validate_artifact_reference(
        record.get("active"),
        expected_path=path,
        state_root=state_root,
        mutable=True,
        max_bytes=None,
    )
    try:
        with _verified_active_database(
            record, database_name=database_name, path=path,
            state_root=state_root, deadline=deadline,
        ) as database:
            yield database
    except sqlite3.OperationalError as error:
        raise_adoption_sqlite_error(error, deadline)


@contextlib.contextmanager
def _verified_active_database(record: dict[str, object], *, database_name: str, path: Path, state_root: Path, deadline: float | None) -> Iterator[sqlite3.Connection]:
    with contextlib.closing(
        open_adopted_database(
            database_name, path, state_root, deadline=deadline,
        )
    ) as database:
        database.execute("BEGIN")
        if not database_schema_complete(database_name, database):
            raise ValueError("active database schema is incomplete")
        _require_database_metadata(record, _active_database_metadata(database))
        operational_deadline_active(deadline)
        yield database
        operational_deadline_active(deadline)


def _sqlite_interrupted(error: sqlite3.OperationalError) -> bool:
    return getattr(error, "sqlite_errorcode", 0) & 0xff == 9  # SQLITE_INTERRUPT.


def raise_adoption_sqlite_error(error: sqlite3.OperationalError, deadline: float | None) -> None:
    if not _sqlite_interrupted(error):
        raise error
    if deadline is None:
        raise error
    operational_deadline_active(deadline)
    raise error


def _active_database_metadata(database: sqlite3.Connection) -> dict[str, object]:
    return {
        name: database.execute(f"PRAGMA {name}").fetchone()[0]
        for name in (
            "application_id",
            "user_version",
            "journal_mode",
            "synchronous",
            "foreign_keys",
            "trusted_schema",
        )
    }


def _require_active_database_openable(record: dict[str, object], *, database_name: str, path: Path, state_root: Path, deadline: float | None = None) -> None:
    """Verify file identity, schema and connection contract without certifying history."""
    with _active_database_snapshot(
        record, database_name=database_name, path=path, state_root=state_root, deadline=deadline
    ):
        pass


def validate_active_database_reference(record: dict[str, object], *, database_name: str, path: Path, state_root: Path, deadline: float | None = None) -> None:
    """Full certification additionally checks every retained page and foreign key."""
    with _active_database_snapshot(
        record, database_name=database_name, path=path, state_root=state_root, deadline=deadline
    ) as database:
        certify_active_database_contents(database_name, database)
        if database_name == "queue":
            audit_compile_progress(database, state_root, deadline=deadline)


def certify_active_database_contents(database_name: str, database: sqlite3.Connection) -> None:
    if database_name == "coordinator":
        require_v3_invariants(database)
        return
    integrity = database.execute("PRAGMA integrity_check").fetchall()
    foreign_keys = database.execute("PRAGMA foreign_key_check").fetchall()
    _require_database_health(True, integrity, foreign_keys)


def _database_contract(database_name: str) -> OperationalDatabaseContract:
    if database_name == "queue":
        return _QUEUE_CONTRACT
    if database_name == "coordinator":
        return COORDINATOR_CONTRACT
    raise ValueError("unknown operational database")


def database_schema_complete(database_name: str, database: sqlite3.Connection) -> bool:
    if database_name == "queue":
        return queue_v3_schema_complete(database)
    if database_name == "coordinator":
        return coordinator_v3_schema_complete(database)
    raise ValueError("unknown operational database")


def _require_database_health(
    complete: bool, integrity: list[sqlite3.Row], foreign_keys: list[sqlite3.Row]
) -> None:
    failures = (
        (not complete, "active database schema is incomplete"),
        (
            len(integrity) != 1 or integrity[0][0] != "ok",
            "active database integrity check failed",
        ),
        (bool(foreign_keys), "active database foreign key check failed"),
    )
    for failed, message in failures:
        if failed:
            raise ValueError(message)


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


def validate_complete_adoption(
    *,
    root: Path,
    state_root: Path,
    paths: dict[str, Path],
    migration: dict[str, object],
    adoption: dict[str, object],
    operation_artifacts: list[str],
    active_validator: Callable[..., None] = validate_active_database_reference,
    deadline: float | None = None,
) -> dict[str, object]:
    if adoption.get("schema_version") == "reliability-v3-adoption/v2":
        return _validate_upgraded_adoption(
            root=root, state_root=state_root, paths=paths, migration=migration,
            adoption=adoption, operation_artifacts=operation_artifacts,
            active_validator=active_validator, deadline=deadline,
        )
    operational_deadline_active(deadline)
    _require_adoption_artifacts_complete(paths, operation_artifacts)
    _require_adoption_header(adoption, migration)
    _require_migration_reference(adoption, paths, state_root)
    migration_by_name = named_database_records(migration.get("databases"))
    adoption_by_name = named_database_records(adoption.get("databases"))
    schemas = _require_adoption_sources(root, adoption)
    for database_name in ("queue", "coordinator"):
        operational_deadline_active(deadline)
        _validate_adopted_database(
            active_validator=active_validator,
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


def _upgraded_active_validator(record, *, upgraded_active, validator, database_name, **kwargs):
    if database_name == "queue":
        record = dict(record, active=upgraded_active, user_version=QUEUE_COMPILE_CONTRACT.user_version)
    return validator(record, database_name=database_name, **kwargs)


def _validate_upgraded_adoption(*, adoption, active_validator, **kwargs):
    validate_upgrade_envelope(adoption)
    validator = partial(
        _upgraded_active_validator, upgraded_active=adoption["queue_active"],
        validator=active_validator,
    )
    validate_complete_adoption(
        adoption=adoption["previous"], active_validator=validator, **kwargs,
    )
    return adoption


def _require_adoption_artifacts_complete(
    paths: dict[str, Path], operation_artifacts: list[str]
) -> None:
    if operation_artifacts:
        raise ValueError("operation artifacts remain after adoption")
    if any(
        kind(paths[name]) != "missing"
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
        paths["migration"], state_root, max_bytes=MAX_RECORD_BYTES, owner_only=True
    )
    expected = {
        "path": "run/reliability-v3-migration.json",
        "sha256": sha256_bytes(migration_bytes),
    }
    if adoption.get("migration") != expected:
        raise ValueError("adoption migration reference changed")


def named_database_records(value: object) -> dict[str, dict[str, object]]:
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
    name = _database_record_name(item)
    if name in existing:
        raise ValueError("adoption database name is duplicated")
    return name, item


def _database_record_name(item: object) -> str:
    if not isinstance(item, dict):
        raise ValueError("adoption database record is invalid")
    name = item.get("database")
    if not isinstance(name, str):
        raise ValueError("adoption database name is invalid")
    return name


def expected_schema_digests() -> dict[str, str]:
    return {
        "queue_schema_sha256": QUEUE_V3_SCHEMA_SHA256,
        "coordinator_schema_sha256": COORDINATOR_V3_SCHEMA_SHA256,
        "adoption_schema_sha256": sha256_bytes(ADOPTION_SCHEMA.read_bytes()),
    }


def current_schema_digests() -> dict[str, str]:
    return dict(expected_schema_digests(),
                queue_schema_sha256=QUEUE_COMPILE_SCHEMA_SHA256,
                adoption_schema_sha256=sha256_bytes(_FRESH_ADOPTION_SCHEMA.read_bytes()))


def initial_schema_digests(source_state):
    """The schema digests a new adoption of a vault in ``source_state`` records."""
    if source_state == "fresh":
        return current_schema_digests()
    return expected_schema_digests()


def validated_schema_digests(record):
    schemas = record.get("schemas")
    if schemas == expected_schema_digests():
        return schemas
    if record.get("source_state") == "fresh" and schemas == current_schema_digests():
        return schemas
    raise ValueError("adoption schema digests changed")


def adoption_schema(record):
    schemas = {"reliability-v3-adoption/v1": ADOPTION_SCHEMA,
               "reliability-v3-adoption/v3": _FRESH_ADOPTION_SCHEMA}
    version = record.get("schema_version")
    if version not in schemas:
        raise ValueError("unsupported adoption schema version")
    return schemas[version]


def _validate_direct_adoption(value):
    _bounded_adoption_component(value)
    validate_schema(value, adoption_schema(value))
    expected = expected_schema_digests()
    if value["schema_version"] == "reliability-v3-adoption/v3":
        expected = current_schema_digests()
    if value.get("schemas") != expected:
        raise ValueError("adoption schema digests changed")


def _require_adoption_sources(
    root: Path, adoption: dict[str, object]
) -> dict[str, object]:
    """Validate what an adopted record must still agree with on every read.

    The schema digests stay fail-closed. A v3 database whose shape no longer
    matches the code about to read it is a real incompatibility, and the check
    costs one comparison of two recorded constants.

    ``installed_integration_sha256`` is deliberately *not* re-checked here. It
    is provenance -- which producer performed the cutover -- and it is bound
    where that binding decides something: `_validate_migration_context` refuses
    an adoption whose plan was made against different adapter bytes, so the
    window between `--plan` and `--apply` is still closed. Re-checking it on
    every validation turned any later edit of `scripts/integration_adapter.py`
    into a total outage of the memory write path -- capture, queue and every
    Markdown transaction -- because `require_reliability_v3_adopted` guards all
    of them. The nightly fast-forward the owner approved on 2026-08-23 updates
    this checkout unattended, so the first update touching the adapter would
    have disabled memory silently. A digest can say "different"; it cannot say
    "incompatible", and the incompatibility that matters is the schema, which
    is checked above. See
    `knowledge/notes/adoption-digest-is-provenance-decision.md`.

    What replaces it is the property that is actually checkable and actionable:
    the producer must still be installed. An absent adapter is a broken
    installation and fails closed; a changed one is an ordinary update.
    """
    schemas = validated_schema_digests(adoption)
    if kind(root / "scripts" / "integration_adapter.py") != "file":
        raise ValueError("installed integration is missing")
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


def expected_migration_record(
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


def expected_tombstone(
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
    active_validator: Callable[..., None] = validate_active_database_reference,
) -> None:
    expected_migration = expected_migration_record(
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
    _validate_adopted_active(
        database_name, paths, state_root, adoption_record, active_validator=active_validator
    )
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
    tombstone_bytes = validate_artifact_reference(
        adoption_record.get("tombstone"),
        expected_path=paths[legacy_key],
        state_root=state_root,
        mutable=False,
        max_bytes=MAX_TOMBSTONE_BYTES,
    )
    tombstone = read_record(
        paths[legacy_key], state_root, schema=TOMBSTONE_SCHEMA, max_bytes=MAX_TOMBSTONE_BYTES
    )
    if tombstone_bytes != canonical_json_bytes(tombstone):
        raise ValueError("tombstone bytes are not canonical")
    expected = expected_tombstone(
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
    *,
    active_validator: Callable[..., None] = validate_active_database_reference,
) -> None:
    _legacy, _active, _retired, _legacy_key, active_key, _retired_key = _database_spec(
        database_name
    )
    active_validator(
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
    if "retired" in adoption_record or kind(paths[retired_key]) != "missing":
        raise ValueError("fresh adoption contains retired database evidence")


def _validate_upgrade_retired(
    path: Path,
    state_root: Path,
    migration_record: dict[str, object],
    adoption_record: dict[str, object],
) -> None:
    retired_bytes = validate_artifact_reference(
        adoption_record.get("retired"),
        expected_path=path,
        state_root=state_root,
        mutable=False,
        max_bytes=MAX_OPERATIONAL_DB_BYTES,
    )
    if sha256_bytes(retired_bytes or b"") != migration_record.get("source_sha256"):
        raise ValueError("retired database differs from migration source")


class _StraySpec(NamedTuple):
    """One pre-adoption candidate: where it lives and what makes it stray."""

    key: str
    contract: OperationalDatabaseContract
    data_tables: tuple[str, ...]
    owner_table: str
    owner_label: str
    quarantine: str


# A candidate is stray when none of these holds a row and no owner is live. The
# epoch tables only count and are left out, as `doctor` always left them out.
STRAY_SPECS = (
    _StraySpec(
        "coordinator_candidate",
        COORDINATOR_CONTRACT,
        (
            "transaction",
            "operation",
            "intent_fences",
            "project_leases",
            "writer_owners",
            "project_checkpoints",
            "blackboard_claims",
            "capture_binding_projections",
        ),
        "maintenance_owners",
        "maintenance owner",
        "coordinator-quarantine",
    ),
    _StraySpec(
        "queue_candidate",
        _QUEUE_CONTRACT,
        (
            "tasks",
            "attempt_history",
            "capture_intents",
            "capture_task_links",
            "semantic_decisions",
            "source_fences",
            "task_fences",
            "task_source_links",
        ),
        "queue_ownership",
        "queue owner",
        "queue-quarantine",
    ),
)


STRAY_CANDIDATE_NAMES = (
    "markdown-transactions-v3.candidate.sqlite3",
    "queue-v3.candidate.sqlite3",
)


class StrayRetirement(NamedTuple):
    """What `retire_stray_candidates` moved, and why it left anything in place."""

    retired: tuple[str, ...]
    kept: tuple[str, ...]


def _adoption_in_flight(paths: dict[str, Path], *, deadline: float | None = None) -> str | None:
    """Why no candidate may be retired now, or None when adoption is complete."""
    if kind(paths["adoption"]) != "file":
        return "no complete adoption record"
    artifacts, _overflow = listed_operation_artifacts(paths["run"], deadline=deadline)
    if artifacts:
        return "an adoption operation is in flight: " + ", ".join(artifacts)
    return None


def _first_held_table(database: sqlite3.Connection, spec: _StraySpec, *, deadline: float | None = None) -> str | None:
    for table in spec.data_tables:
        operational_deadline_active(deadline)
        if database.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone() is not None:
            return f"holds a row in {table}"
    return None


def _live_owner(database: sqlite3.Connection, spec: _StraySpec, now: datetime, *, deadline: float | None = None) -> str | None:
    from operational_ownership import parse_timestamp

    for actor, expires in database.execute(
        f'SELECT actor_id, expires_at FROM "{spec.owner_table}"'
    ):
        operational_deadline_active(deadline)
        if expires is not None and parse_timestamp(expires) > now:
            return f"{spec.owner_label} {actor} is live until {expires}"
    return None


def _content_reason(candidate: Path, state_root: Path, spec: _StraySpec, now: datetime, *, deadline: float | None = None) -> str | None:
    """Why the candidate's contents forbid retiring it, or None when it is empty."""
    operational_deadline_active(deadline)
    if kind(candidate.with_name(candidate.name + "-journal")) != "missing":
        return "a rollback journal is beside it"
    try:
        with contextlib.closing(
            sqlite_io.open_readonly_operational_db(
                candidate, state_root, max_bytes=MAX_OPERATIONAL_DB_BYTES, contract=spec.contract, deadline=deadline
            )
        ) as database:
            reason = _first_held_table(database, spec, deadline=deadline) or _live_owner(database, spec, now, deadline=deadline)
            operational_deadline_active(deadline)
            return reason
    except Exception as exc:  # noqa: BLE001 - unreadable means keep, and say why
        operational_deadline_active(deadline)
        return f"its contents could not be read: {type(exc).__name__}: {exc}"


def _quarantined(candidate: Path, directory: Path, now: datetime) -> Path:
    """Rename the candidate into an owner-only quarantine directory; never delete it."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    destination = directory / f"{now.strftime('%Y%m%dT%H%M%SZ')}-{candidate.name}"
    candidate.rename(destination)
    return destination


def _retire_one(
    paths: dict[str, Path], spec: _StraySpec, now: datetime, blocker: str | None, *, deadline: float | None = None
) -> tuple[str, str]:
    operational_deadline_active(deadline)
    candidate = paths[spec.key]
    if kind(candidate) != "file":
        return "kept", f"{candidate.name}: not a regular file"
    reason = blocker or _content_reason(candidate, paths["run"].parent, spec, now, deadline=deadline)
    if reason is not None:
        return "kept", f"{candidate.name}: {reason}"
    operational_deadline_active(deadline)
    try:
        destination = _quarantined(candidate, paths["run"] / spec.quarantine, now)
    except FileNotFoundError:
        return "gone", candidate.name
    return "retired", destination.relative_to(paths["run"].parent).as_posix()


def retire_stray_candidates(state_root: Path, now: datetime, *, deadline: float | None = None) -> StrayRetirement:
    """Quarantine every pre-adoption candidate that is provably stray.

    A stray candidate refuses every writer; on 2026-09-17 one left by a test run
    stopped capture, the nightly and the weekly for six days, and the repair ran
    only when a person ran `doctor --repair`. The rule here is the one doctor
    applied, for both candidates, and it runs where the refusal happens. See
    `docs/research/2026-09-24-a-stray-candidate-is-retired-where-it-refuses.md`.
    """
    operational_deadline_active(deadline)
    paths = adoption_paths(Path(state_root).absolute())
    present = _present_candidates(paths)
    if not present:
        return StrayRetirement((), ())
    blocker = _adoption_in_flight(paths, deadline=deadline)
    outcomes = [_retire_one(paths, spec, now, blocker, deadline=deadline) for spec in present]
    return StrayRetirement(_details(outcomes, "retired"), _details(outcomes, "kept"))


def _present_candidates(paths: dict[str, Path]) -> list[_StraySpec]:
    return [spec for spec in STRAY_SPECS if kind(paths[spec.key]) != "missing"]


def _details(outcomes: list[tuple[str, str]], wanted: str) -> tuple[str, ...]:
    return tuple(detail for kind, detail in outcomes if kind == wanted)


def require_reliability_v3_adopted(*, root: Path, state_root: Path, deadline: float | None = None) -> dict[str, object]:
    """Certify the complete adopted pair, including all retained database content."""
    return _require_adoption_record(root, state_root, validate_active_database_reference, deadline=deadline)


def require_reliability_v3_admission(*, root: Path, state_root: Path, deadline: float | None = None) -> dict[str, object]:
    """Admit an actor from verified adoption evidence and both database contracts."""
    return _require_adoption_record(root, state_root, _require_active_database_openable, deadline=deadline)


def _require_adoption_record(root: Path, state_root: Path, active_validator: Callable[..., None], *, deadline: float | None = None) -> dict[str, object]:
    operational_deadline_active(deadline)
    bounded_validator = partial(active_validator, deadline=deadline)
    try:
        result = load_complete_adoption(
            root=Path(root), state_root=Path(state_root), active_validator=bounded_validator, deadline=deadline
        )
        operational_deadline_active(deadline)
        return result
    except (ReliabilityV3ValidationError, TimeoutError):
        raise
    except Exception as exc:
        raise ReliabilityV3ValidationError(
            "reliability_v3_record_invalid", f"{type(exc).__name__}: {exc}"
        ) from exc


def load_complete_adoption(
    *,
    root: Path,
    state_root: Path,
    active_validator: Callable[..., None] = validate_active_database_reference,
    deadline: float | None = None,
) -> dict[str, object]:
    operational_deadline_active(deadline)
    vault = root.resolve(strict=True)
    state = state_root.absolute()
    paths = adoption_paths(state)
    operation_artifacts, overflow = listed_operation_artifacts(paths["run"], deadline=deadline)
    if overflow:
        raise ValueError("operation artifact scan exceeded its bound")
    if kind(paths["migration"]) != "file" or kind(paths["adoption"]) != "file":
        raise ReliabilityV3ValidationError("legacy_protocol_unquiesced")
    operational_deadline_active(deadline)
    migration = read_record(
        paths["migration"], state, schema=MIGRATION_SCHEMA, max_bytes=MAX_RECORD_BYTES
    )
    operational_deadline_active(deadline)
    adoption = read_adoption_record(paths["adoption"], state)
    operational_deadline_active(deadline)
    return validate_complete_adoption(
        deadline=deadline,
        root=vault,
        state_root=state,
        paths=paths,
        migration=migration,
        adoption=adoption,
        operation_artifacts=operation_artifacts,
        active_validator=active_validator,
    )


def parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("operational timestamp is not a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("operational timestamp lacks a timezone")
    return parsed.astimezone(UTC)


def adopted_database_path(*, database_name: str, state_root: Path) -> Path:
    """The database a reader should open for ``database_name``.

    After adoption the legacy path is a JSON tombstone, not a database. A
    reader that opens it anyway reports the runtime as unreadable -- which is
    exactly what doctor did on a healthy adopted vault: `queue_state_unreadable`
    and `transaction_state_unreadable`, both of which also block deleting
    `run/`. The tombstone already names its replacement, so follow it.

    Anything that is not a valid tombstone for this database -- a real v2
    database, a partly written record, an unreadable file -- returns the legacy
    path unchanged, so an un-adopted or damaged runtime keeps the verdict it
    had instead of silently being judged from somewhere else.
    """
    legacy, active = _database_spec(database_name)[:2]
    state = Path(state_root).absolute()
    tombstone = _tombstone_document(state / legacy, state)
    if tombstone is None:
        return state / legacy
    names_this_database = (
        tombstone.get("database") == database_name
        and tombstone.get("legacy_path") == legacy
        and tombstone.get("replacement_path") == active
    )
    return state / active if names_this_database else state / legacy


def _tombstone_document(path: Path, state_root: Path) -> dict[str, object] | None:
    """The tombstone at ``path``, or None when it is not one or cannot be read."""
    if kind(path) != "file":
        return None
    try:
        payload = read_runtime_bytes(
            path, state_root, max_bytes=MAX_TOMBSTONE_BYTES, owner_only=True
        )
        document = json.loads(payload)
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    expected = "operational-db-tombstone/v1"
    return document if document.get("schema_version") == expected else None
