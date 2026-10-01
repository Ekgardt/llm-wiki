"""Explicit, fenced and resumable journal-mode migration for adopted v3 state."""
from __future__ import annotations

import argparse
import contextlib
import copy
import json
from collections.abc import Callable
from pathlib import Path

import installed_memory_repair as repair
from operational_ownership import OwnershipRegistry
from reliable_memory import (
    OPERATIONAL_JOURNAL_PENDING,
    OperationalDatabaseContract,
    canonical_json_bytes,
    capture_runtime_file_identity,
    fsync_directory,
    open_operational_db,
    publish_runtime_file,
    read_runtime_bytes,
    require_safe_wal_runtime,
    sha256_bytes,
    validate_schema,
    validate_schema_object,
    validate_state_root,
)

_RECORD_KEYS = frozenset(("migration", "adoption", "queue", "coordinator"))
# Four existing bounded records plus their envelope; no source bound is reduced.
_PLAN_MAX_BYTES = 5 * repair._MAX_RECORD_BYTES


def _record_paths(state: Path) -> dict[str, Path]:
    paths = repair._paths(state)
    return {
        "migration": paths["migration"], "adoption": paths["adoption"],
        "queue": paths["queue_legacy"], "coordinator": paths["coordinator_legacy"],
    }


def _read_json(path: Path, state: Path, limit: int) -> dict:
    raw = read_runtime_bytes(path, state, max_bytes=limit, owner_only=True)
    value = json.loads(raw, object_pairs_hook=repair._unique_object,
                       parse_constant=repair._reject_constant)
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        raise ValueError("migration record must be a canonical JSON object")
    return value


def _source_records(state: Path) -> dict:
    return {name: _read_json(path, state, repair._MAX_RECORD_BYTES)
            for name, path in _record_paths(state).items()}


def _new_plan(root: Path, state: Path, mode: str) -> dict:
    return {
        "schema_version": "operational-journal-migration/v1",
        "root": str(root), "state_root": str(state), "target_mode": mode,
        "source": _source_records(state),
    }


_PLAN_SCHEMA = {
    "type": "object",
    "required": ["schema_version", "root", "state_root", "target_mode", "source"],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"const": "operational-journal-migration/v1"},
        "root": {"type": "string"}, "state_root": {"type": "string"},
        "target_mode": {"enum": ["wal", "delete"]},
        "source": {"type": "object", "required": sorted(_RECORD_KEYS),
                   "additionalProperties": False,
                   "properties": {key: {"type": "object"} for key in _RECORD_KEYS}},
    },
}


def _validate_plan(plan: dict, root: Path, state: Path, mode: str) -> None:
    validate_schema_object(plan, _PLAN_SCHEMA)
    binding = (plan["schema_version"], plan["root"], plan["state_root"], plan["target_mode"])
    if binding != ("operational-journal-migration/v1", str(root), str(state), mode):
        raise ValueError("journal migration plan belongs to a different operation")
    _validate_source_records(plan["source"])
    repair._require_adoption_sources(root, plan["source"]["adoption"])
    _validate_databases(plan["source"]["adoption"], state)
    _validate_publication_state(plan, state)


def _validate_source_records(source: dict) -> None:
    if not isinstance(source, dict) or set(source) != _RECORD_KEYS:
        raise ValueError("journal migration source set is incomplete")
    adoption, migration = source["adoption"], source["migration"]
    validate_schema(migration, repair._MIGRATION_SCHEMA)
    validate_schema(adoption, repair._adoption_schema(migration))
    repair._require_adoption_header(adoption, migration)
    expected = {"path": "run/reliability-v3-migration.json",
                "sha256": sha256_bytes(canonical_json_bytes(migration))}
    if adoption["migration"] != expected:
        raise ValueError("source migration binding differs")
    for record in adoption["databases"]:
        _validate_source_tombstone(source, record)


def _validate_source_tombstone(source: dict, record: dict) -> None:
    name = record["database"]
    tombstone = source[name]
    validate_schema(tombstone, repair._TOMBSTONE_SCHEMA)
    if sha256_bytes(canonical_json_bytes(tombstone)) != record["tombstone"]["sha256"]:
        raise ValueError("source tombstone binding differs")
    adoption = source["adoption"]
    migrations = repair._named_database_records(source["migration"]["databases"])
    expected = repair._expected_tombstone(
        database_name=name, source_state=adoption["source_state"],
        operation_id=adoption["operation_id"],
        adoption_schema_sha256=adoption["schemas"]["adoption_schema_sha256"],
        migration_record=migrations[name],
    )
    if tombstone != expected:
        raise ValueError("source tombstone differs from its migration")


def _validate_databases(adoption: dict, state: Path) -> None:
    records = repair._named_database_records(adoption["databases"])
    paths = repair._paths(state)
    for spec in repair._DATABASE_SPECS:
        repair._validate_artifact_reference(
            records[spec.name]["active"], expected_path=paths[spec.active_key],
            state_root=state, mutable=True, max_bytes=repair._MAX_OPERATIONAL_DB_BYTES,
        )
        spec.validate(paths[spec.active_key], state_root=state)


def _registry(state: Path) -> OwnershipRegistry:
    return OwnershipRegistry._from_adopted_database(
        state, repair._paths(state)["coordinator_active"], journal_migration=True
    )


def _save_plan(path: Path, plan: dict, state: Path) -> None:
    publish_runtime_file(path, canonical_json_bytes(plan), state_root=state, create_only=True)


def _prepare(root: Path, state: Path, mode: str) -> dict | None:
    pending = state / OPERATIONAL_JOURNAL_PENDING
    if pending.exists():
        plan = _read_json(pending, state, _PLAN_MAX_BYTES)
        _validate_plan(plan, root, state, mode)
        return plan
    adoption = repair.require_reliability_v3_adopted(root=root, state_root=state)
    if {item["pragmas"]["journal_mode"] for item in adoption["databases"]} == {mode}:
        return None
    return _new_plan(root, state, mode)


def _schema_for_mode(mode: str) -> Path:
    return {"delete": repair._ADOPTION_SCHEMA, "wal": repair._WAL_ADOPTION_SCHEMA}[mode]


def _target_migration(plan: dict) -> dict:
    migration = copy.deepcopy(plan["source"]["migration"])
    migration["schemas"] = repair._expected_schema_digests(_schema_for_mode(plan["target_mode"]))
    return migration


def _target_tombstone(plan: dict, name: str, migration: dict) -> dict:
    tombstone = copy.deepcopy(plan["source"][name])
    tombstone["adoption_schema_sha256"] = migration["schemas"]["adoption_schema_sha256"]
    return tombstone


def _validate_publication_state(plan: dict, state: Path) -> None:
    targets = {"migration": _target_migration(plan)}
    for name in ("queue", "coordinator"):
        targets[name] = _target_tombstone(plan, name, targets["migration"])
    targets["adoption"] = _target_adoption(plan, targets["migration"], state)
    for name, path in _record_paths(state).items():
        current = _read_json(path, state, repair._MAX_RECORD_BYTES)
        _require_known_record(current, plan["source"][name], targets[name], path)


def _require_known_record(current: dict, old: dict, new: dict, path: Path) -> None:
    if current not in (old, new):
        raise ValueError(f"unexpected migration preimage: {path.name}")


def _replace_known(path: Path, old: dict, new: dict, state: Path) -> None:
    current = _read_json(path, state, repair._MAX_RECORD_BYTES)
    if current == new:
        return
    if current != old:
        raise ValueError(f"unexpected migration preimage: {path.name}")
    publish_runtime_file(
        path, canonical_json_bytes(new), state_root=state, create_only=False,
        expected=capture_runtime_file_identity(path, state_root=state),
        expected_sha256=sha256_bytes(canonical_json_bytes(current)),
    )


def _switch_database(spec, plan: dict, state: Path) -> None:
    record = repair._named_database_records(plan["source"]["adoption"]["databases"])[spec.name]
    contract = OperationalDatabaseContract(application_id=record["application_id"])
    path = repair._paths(state)[spec.active_key]
    with contextlib.closing(open_operational_db(path, busy_ms=0, contract=contract, journal_migration=True)) as db:
        mode = plan["target_mode"]
        observed = db.execute(f"PRAGMA journal_mode={mode}").fetchone()[0]
        if observed != mode:
            raise RuntimeError(f"SQLite did not switch {spec.name} to {mode}")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"integrity failure after switching {spec.name}")
    spec.validate(path, state_root=state)


def _target_adoption(plan: dict, migration: dict, state: Path) -> dict:
    adoption = copy.deepcopy(plan["source"]["adoption"])
    adoption["schema_version"] = {"delete": "reliability-v3-adoption/v1",
                                  "wal": "reliability-v3-adoption/v2"}[plan["target_mode"]]
    adoption["schemas"] = migration["schemas"]
    adoption["migration"]["sha256"] = sha256_bytes(canonical_json_bytes(migration))
    paths = _record_paths(state)
    for record in adoption["databases"]:
        record["pragmas"]["journal_mode"] = plan["target_mode"]
        record["tombstone"] = repair._artifact_record(
            paths[record["database"]], state, repair._MAX_TOMBSTONE_BYTES
        )
    return adoption


def _apply_records(plan: dict, state: Path, emit: Callable[[str], None]) -> None:
    paths = _record_paths(state)
    migration = _target_migration(plan)
    for name in ("queue", "coordinator"):
        target = _target_tombstone(plan, name, migration)
        _replace_known(paths[name], plan["source"][name], target, state)
        emit(f"{name}_tombstone")
    _replace_known(paths["migration"], plan["source"]["migration"], migration, state)
    emit("migration_record")
    adoption = _target_adoption(plan, migration, state)
    validate_schema(adoption, _schema_for_mode(plan["target_mode"]))
    _replace_known(paths["adoption"], plan["source"]["adoption"], adoption, state)
    emit("adoption_record")


def _finish(plan: dict, root: Path, state: Path) -> None:
    repair._read_complete_adoption(root=root, state_root=state)
    payload = canonical_json_bytes(plan)
    archive = state / "run/install" / f"operational-journal-{sha256_bytes(payload)}.json"
    if archive.exists():
        if _read_json(archive, state, _PLAN_MAX_BYTES) != plan:
            raise ValueError("migration archive conflict")
    else:
        _save_plan(archive, plan, state)
    (state / OPERATIONAL_JOURNAL_PENDING).unlink()
    fsync_directory(archive.parent)


def migrate(root: Path, state_root: Path, *, mode: str = "wal",
            emit: Callable[[str], None] = lambda _event: None) -> dict:
    """Run offline after all old clients have stopped; restartable at every emit."""
    root, state = Path(root).resolve(strict=True), Path(state_root).resolve(strict=True)
    _schema_for_mode(mode)
    require_safe_wal_runtime()
    plan = _prepare(root, state, mode)
    if plan is None:
        return {"status": "already_current", "journal_mode": mode}
    return _run_fenced(plan, root, state, emit)


def _run_fenced(plan: dict, root: Path, state: Path, emit: Callable[[str], None]) -> dict:
    registry = _registry(state)
    lease = registry.acquire("runtime-deletion-check", scope="operational-journal-migration")
    try:
        _validate_plan(plan, root, state, plan["target_mode"])
        pending = state / OPERATIONAL_JOURNAL_PENDING
        validate_state_root(pending.parent)
        _ensure_pending(pending, plan, state)
        def checkpoint(event):
            registry.heartbeat(lease)
            emit(event)

        checkpoint("prepared")
        for spec in repair._DATABASE_SPECS:
            _switch_database(spec, plan, state)
            checkpoint(f"{spec.name}_database")
        _apply_records(plan, state, checkpoint)
        registry.heartbeat(lease)
        _finish(plan, root, state)
    finally:
        registry.release(lease)
    return {"status": "completed", "journal_mode": plan["target_mode"]}


def _ensure_pending(path: Path, plan: dict, state: Path) -> None:
    if path.exists():
        if _read_json(path, state, _PLAN_MAX_BYTES) != plan:
            raise ValueError("pending migration changed")
        return
    _save_plan(path, plan, state)


def journal_migration_pending(install_root: Path) -> bool:
    pending = install_root / OPERATIONAL_JOURNAL_PENDING.name
    if not pending.exists():
        return False
    plan = _read_json(pending, install_root.parent.parent, _PLAN_MAX_BYTES)
    validate_schema_object(plan, _PLAN_SCHEMA)
    _validate_source_records(plan["source"])
    return True


def retained_journal_entries(install_root: Path) -> set[str]:
    """Validate retained migration evidence, including copied backup evidence."""
    state = install_root.parent.parent
    entries = set()
    for path in install_root.glob("operational-journal-*.json"):
        _validate_retained_plan(path, state)
        entries.add(path.name)
    return entries


def _validate_retained_plan(path: Path, state: Path) -> None:
    if path.name == OPERATIONAL_JOURNAL_PENDING.name:
        raise ValueError("operational journal migration is pending")
    plan = _read_json(path, state, _PLAN_MAX_BYTES)
    validate_schema_object(plan, _PLAN_SCHEMA)
    expected = f"operational-journal-{sha256_bytes(canonical_json_bytes(plan))}.json"
    if path.name != expected:
        raise ValueError("retained journal migration digest differs")
    _schema_for_mode(plan["target_mode"])
    _validate_source_records(plan["source"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--mode", choices=("wal", "delete"), default="wal")
    parser.add_argument("--confirm-all-agents-stopped", required=True, action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(args.root, args.state_root, mode=args.mode), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
