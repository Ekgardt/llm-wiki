from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from pathlib import Path

import markdown_transaction
import memory_queue
import pytest
from installed_memory_repair import (
    inspect_installed_vault,
    repair_installed_vault,
    require_reliability_v3_adopted,
)
from markdown_transaction import MarkdownCoordinator
from memory_queue import MemoryQueue
from reliable_memory import (
    canonical_json_bytes,
    capture_runtime_file_identity,
    sha256_bytes,
)

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
    memory_queue._harden_owner_only(path, 0o600)
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
    memory_queue.initialize_queue_v3_candidate(queue_path, source_v2=None)
    markdown_transaction.initialize_coordinator_v3_candidate(
        coordinator_path, source_v2=None
    )
    operation_id = f"reliability-v3:{'a' * 64}"
    schemas = {
        "queue_schema_sha256": memory_queue.QUEUE_V3_SCHEMA_SHA256,
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
            memory_queue.validate_queue_v3_database(active_path, state_root=state_root)
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


def test_empty_vault_inspection_is_read_only_and_reports_fresh(tmp_path: Path) -> None:
    root, state_root = _vault(tmp_path)

    report = inspect_installed_vault(root=root, state_root=state_root)

    assert report == {
        "mode": "check",
        "overall_status": "degraded",
        "actions": [],
        "blockers": [{"code": "reliability_v3_runtime_activation_incomplete"}],
        "details": {
            "adoption_state": "fresh",
            "artifacts": {},
        },
    }
    assert not state_root.exists()


def test_existing_v2_pair_is_upgrade_required_without_mutation(tmp_path: Path) -> None:
    root, state_root = _vault(tmp_path)
    queue = MemoryQueue(state_root)
    queue.enqueue("query", 1, {"prompt": "retained"})
    MarkdownCoordinator(root, state_root)
    before = _inventory(state_root)

    report = inspect_installed_vault(root=root, state_root=state_root)

    assert report["overall_status"] == "degraded"
    assert report["details"]["adoption_state"] == "upgrade-required"
    assert report["blockers"] == [
        {"code": "reliability_v3_runtime_activation_incomplete"}
    ]
    assert _inventory(state_root) == before


@pytest.mark.parametrize(
    "legacy_artifact",
    [
        "run/queue/pending.json",
        "run/queue-migrated-v2",
        "run/transactions/tx/plan.json",
        "run/queue-results/result.json",
        "run/queue-quarantine/package/manifest.json",
    ],
)
def test_legacy_operational_artifacts_prevent_fresh_classification(
    tmp_path: Path,
    legacy_artifact: str,
) -> None:
    root, state_root = _vault(tmp_path)
    artifact = state_root / legacy_artifact
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    before = _inventory(state_root)

    report = inspect_installed_vault(root=root, state_root=state_root)

    assert report["overall_status"] == "error"
    assert report["details"]["adoption_state"] == "conflict"
    assert {item["code"] for item in report["blockers"]} >= {
        "legacy_operational_evidence_present"
    }
    assert _inventory(state_root) == before


def test_partial_v3_artifacts_are_reported_without_opening_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, state_root = _vault(tmp_path)
    run = state_root / "run"
    run.mkdir(parents=True)
    migration = run / "reliability-v3-migration.json"
    migration.write_text("{}", encoding="utf-8")
    before = _inventory(state_root)

    def reject_writable_open(*_args: object, **_kwargs: object) -> None:
        pytest.fail("read-only inspection opened an operational database writable")

    monkeypatch.setattr(sqlite3, "connect", reject_writable_open)

    report = inspect_installed_vault(root=root, state_root=state_root)

    assert report["overall_status"] == "error"
    assert report["details"]["adoption_state"] == "conflict"
    assert report["blockers"] == [{"code": "reliability_v3_record_invalid"}]
    assert _inventory(state_root) == before


def test_offline_apply_adopts_v2_pair_and_retains_exact_sources(tmp_path: Path) -> None:
    root, state_root = _vault(tmp_path)
    queue = MemoryQueue(state_root)
    queue.enqueue("query", 1, {"prompt": "retained"})
    MarkdownCoordinator(root, state_root)
    queue_source = state_root / "run/queue.sqlite3"
    coordinator_source = state_root / "run/markdown-transactions.sqlite3"
    source_bytes = {
        "queue": queue_source.read_bytes(),
        "coordinator": coordinator_source.read_bytes(),
    }

    report = repair_installed_vault(
        root=root,
        state_root=state_root,
        adopt_ownership_v3=True,
        confirm_all_agents_stopped=True,
    )

    assert report == {
        "mode": "apply",
        "overall_status": "ok",
        "actions": [{"code": "reliability_v3_adopted"}],
        "blockers": [],
        "details": {"adoption_state": "adopted"},
    }
    assert (state_root / "run/queue-v2-retired.sqlite3").read_bytes() == source_bytes[
        "queue"
    ]
    assert (
        state_root / "run/markdown-transactions-v2-retired.sqlite3"
    ).read_bytes() == source_bytes["coordinator"]
    for legacy_path in (queue_source, coordinator_source):
        assert json.loads(legacy_path.read_bytes())["schema_version"] == (
            "operational-db-tombstone/v1"
        )
    queue_validation = memory_queue.validate_queue_v3_database(
        state_root / "run/queue-v3.sqlite3", state_root=state_root
    )
    coordinator_validation = markdown_transaction.validate_coordinator_v3_database(
        state_root / "run/markdown-transactions-v3.sqlite3", state_root=state_root
    )
    assert queue_validation["row_counts"]["tasks"] == 1
    assert coordinator_validation["integrity_check"] == "ok"
    assert require_reliability_v3_adopted(root=root, state_root=state_root)[
        "source_state"
    ] == "upgrade"


def test_upgrade_rejects_sqlite_sidecar_before_first_publication(tmp_path: Path) -> None:
    root, state_root = _vault(tmp_path)
    MemoryQueue(state_root)
    MarkdownCoordinator(root, state_root)
    sidecar = state_root / "run/queue.sqlite3-wal"
    sidecar.write_bytes(b"uncheckpointed")
    before = _inventory(state_root)

    report = repair_installed_vault(
        root=root,
        state_root=state_root,
        adopt_ownership_v3=True,
        confirm_all_agents_stopped=True,
    )

    assert report["overall_status"] == "error"
    assert report["blockers"] == [{"code": "reliability_v3_adoption_failed"}]
    assert _inventory(state_root) == before
    assert not (state_root / "run/reliability-v3-migration.json").exists()


def test_upgrade_rejects_leased_v2_task_before_first_publication(tmp_path: Path) -> None:
    root, state_root = _vault(tmp_path)
    queue = MemoryQueue(state_root)
    queue.enqueue("query", 1, {"prompt": "in flight"})
    assert queue.claim("worker") is not None
    MarkdownCoordinator(root, state_root)
    before = _inventory(state_root)

    report = repair_installed_vault(
        root=root,
        state_root=state_root,
        adopt_ownership_v3=True,
        confirm_all_agents_stopped=True,
    )

    assert report["overall_status"] == "error"
    assert report["blockers"] == [{"code": "reliability_v3_adoption_failed"}]
    assert _inventory(state_root) == before
    assert not (state_root / "run/reliability-v3-migration.json").exists()


def test_upgrade_rejects_live_legacy_owner_marker_before_publication(
    tmp_path: Path,
) -> None:
    root, state_root = _vault(tmp_path)
    MemoryQueue(state_root)
    MarkdownCoordinator(root, state_root)
    marker = state_root / "run/compile.pid"
    marker.write_text(f"{os.getpid()}\n2026-08-16T12:00:00\nowner-token\n", encoding="ascii")
    before = _inventory(state_root)

    report = repair_installed_vault(
        root=root,
        state_root=state_root,
        adopt_ownership_v3=True,
        confirm_all_agents_stopped=True,
    )

    assert report["overall_status"] == "error"
    assert _inventory(state_root) == before
    assert not (state_root / "run/reliability-v3-migration.json").exists()


def test_valid_v3_database_artifact_is_inspected_read_only(tmp_path: Path) -> None:
    root, state_root = _vault(tmp_path)
    run = state_root / "run"
    run.mkdir(parents=True)
    database = run / "queue-v3.sqlite3"
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE evidence(value TEXT)")
    before = _inventory(state_root)

    report = inspect_installed_vault(root=root, state_root=state_root)

    assert report["overall_status"] == "error"
    assert report["details"]["adoption_state"] == "conflict"
    assert _inventory(state_root) == before


def test_adopted_inspection_validates_every_immutable_artifact_reference(
    tmp_path: Path,
) -> None:
    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    tombstone_path = state_root / "run/queue.sqlite3"
    tombstone = json.loads(tombstone_path.read_bytes())
    tombstone["operation_id"] = f"reliability-v3:{'f' * 64}"
    _write_runtime_record(tombstone_path, tombstone)
    before = _inventory(state_root)

    report = inspect_installed_vault(root=root, state_root=state_root)

    assert report["overall_status"] == "error"
    assert report["details"]["adoption_state"] == "conflict"
    assert report["blockers"] == [{"code": "reliability_v3_record_invalid"}]
    assert _inventory(state_root) == before


def test_adopted_inspection_rejects_stale_schema_or_integration_digest(
    tmp_path: Path,
) -> None:
    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    adoption_path = state_root / "run/reliability-v3-adopted.json"
    migration_path = state_root / "run/reliability-v3-migration.json"
    adoption = json.loads(adoption_path.read_bytes())
    migration = json.loads(migration_path.read_bytes())
    adoption["schemas"]["queue_schema_sha256"] = "f" * 64
    migration["schemas"]["queue_schema_sha256"] = "f" * 64
    migration_bytes = _write_runtime_record(migration_path, migration)
    adoption["migration"]["sha256"] = sha256_bytes(migration_bytes)
    _write_runtime_record(adoption_path, adoption)

    report = inspect_installed_vault(root=root, state_root=state_root)

    assert report["overall_status"] == "error"
    assert report["blockers"] == [{"code": "reliability_v3_record_invalid"}]
