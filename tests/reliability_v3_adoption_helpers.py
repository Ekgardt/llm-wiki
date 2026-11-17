"""Helpers shared by test_reliability_v3_adoption.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path

import markdown_transaction
import queue_core
from reliable_memory import canonical_json_bytes, capture_runtime_file_identity, sha256_bytes

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _vault(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    for relative in (
        "knowledge/daily",
        "knowledge/notes",
        "knowledge/projects",
        "knowledge/inbox",
        "knowledge/feedback",
    ):
        (root / relative).mkdir(parents=True)
    (root / "knowledge/index.md").write_text("# Index\n", encoding="utf-8")
    (root / "knowledge/log.md").write_text("# Log\n", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts/integration_adapter.py").write_bytes(
        (SCRIPTS_DIR / "integration_adapter.py").read_bytes()
    )
    return root, state_root


def _inventory(path: Path) -> dict[str, tuple[int, int]]:
    if not path.exists():
        return {}
    return {
        item.relative_to(path).as_posix(): (item.stat().st_size, item.stat().st_mtime_ns)
        for item in path.rglob("*")
        if item.is_file()
    }


def _write_runtime_record(path: Path, value: dict[str, object]) -> bytes:
    payload = canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    queue_core.harden_owner_only(path, 0o600)
    return payload


def _artifact(state_root: Path, path: Path) -> dict[str, object]:
    identity = capture_runtime_file_identity(path, state_root=state_root)
    return {
        "path": path.relative_to(state_root).as_posix(),
        "sha256": sha256_bytes(path.read_bytes()),
        "identity": {
            "platform": identity.platform,
            "volume": identity.volume,
            "file_id": identity.file_id,
            "size": identity.size,
            "mtime_ns": identity.mtime_ns,
        },
    }


def build_adopted_reliability_v3(root: Path, state_root: Path) -> None:
    """Build the smallest valid fresh-adoption fixture from real v3 databases."""
    run = state_root / "run"
    queue_path = run / "queue-v3.sqlite3"
    coordinator_path = run / "markdown-transactions-v3.sqlite3"
    queue_core.initialize_queue_v3_candidate(queue_path, source_v2=None)
    markdown_transaction.initialize_coordinator_v3_candidate(
        coordinator_path, source_v2=None
    )
    operation_id = f"reliability-v3:{'a' * 64}"
    schemas = {
        "queue_schema_sha256": queue_core.QUEUE_V3_SCHEMA_SHA256,
        "coordinator_schema_sha256": (
            markdown_transaction.COORDINATOR_V3_SCHEMA_SHA256
        ),
        "adoption_schema_sha256": sha256_bytes(
            (SCRIPTS_DIR / "schemas/reliability-v3-adoption-v1.json").read_bytes()
        ),
    }
    integration_sha256 = sha256_bytes(
        (root / "scripts/integration_adapter.py").read_bytes()
    )
    databases = (
        ("queue", "run/queue.sqlite3", "run/queue-v3.sqlite3", queue_path),
        (
            "coordinator",
            "run/markdown-transactions.sqlite3",
            "run/markdown-transactions-v3.sqlite3",
            coordinator_path,
        ),
    )
    migration = {
        "schema_version": "reliability-v3-migration/v1",
        "operation_id": operation_id,
        "source_state": "fresh",
        "databases": [
            {
                "database": database,
                "legacy_path": legacy,
                "replacement_path": replacement,
            }
            for database, legacy, replacement, _path in databases
        ],
        "schemas": schemas,
        "installed_integration_sha256": integration_sha256,
    }
    migration_path = run / "reliability-v3-migration.json"
    migration_bytes = _write_runtime_record(migration_path, migration)

    adoption_databases: list[dict[str, object]] = []
    for database, legacy, replacement, active_path in databases:
        tombstone = {
            "schema_version": "operational-db-tombstone/v1",
            "database": database,
            "source_state": "fresh",
            "legacy_path": legacy,
            "replacement_path": replacement,
            "operation_id": operation_id,
            "adoption_schema_sha256": schemas["adoption_schema_sha256"],
        }
        tombstone_path = state_root / legacy
        _write_runtime_record(tombstone_path, tombstone)
        validation = (
            queue_core.validate_queue_v3_database(active_path, state_root=state_root)
            if database == "queue"
            else markdown_transaction.validate_coordinator_v3_database(
                active_path, state_root=state_root
            )
        )
        adoption_databases.append(
            {
                "database": database,
                "active": _artifact(state_root, active_path),
                "tombstone": _artifact(state_root, tombstone_path),
                "application_id": validation["application_id"],
                "user_version": validation["user_version"],
                "pragmas": {
                    "journal_mode": validation["journal_mode"],
                    "synchronous": validation["synchronous"],
                    "foreign_keys": 1,
                    "trusted_schema": validation["trusted_schema"],
                },
            }
        )
    adoption = {
        "schema_version": "reliability-v3-adoption/v1",
        "operation_id": operation_id,
        "source_state": "fresh",
        "migration": {
            "path": "run/reliability-v3-migration.json",
            "sha256": sha256_bytes(migration_bytes),
        },
        "databases": adoption_databases,
        "schemas": schemas,
        "installed_integration_sha256": integration_sha256,
    }
    _write_runtime_record(run / "reliability-v3-adopted.json", adoption)
