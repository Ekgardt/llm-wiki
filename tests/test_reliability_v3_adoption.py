from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from pathlib import Path

import markdown_transaction
import pytest
import queue_core
from installed_memory_repair import (
    inspect_installed_vault,
    repair_installed_vault,
)
from llm_wiki.adoption_admission import (
    require_reliability_v3_adopted,
)
from markdown_transaction import MarkdownCoordinator
from queue_core import (
    MemoryQueue,
)
from reliable_memory import (
    sha256_bytes,
)

from tests.reliability_v3_adoption_helpers import (
    _inventory,
    _vault,
    _write_runtime_record,
    build_adopted_reliability_v3,
)


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
    queue_validation = queue_core.validate_queue_v3_database(
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


@pytest.mark.parametrize('name', [
    'coordinator_migration_error', 'coordinator_v3_row_counts',
    'coordinator_v3_base_cross_table_invariant', 'coordinator_v3_blackboard_invariant',
    'coordinator_v3_cross_table_invariant', 'require_v3_invariants',
    'require_v3_integrity', 'v3_operation_violations',
])
def test_coordinator_invariant_aliases_retain_serialized_identities(name):
    import pickle

    from llm_wiki import coordinator_invariants

    value = getattr(coordinator_invariants, name)
    assert getattr(markdown_transaction, name) is value
    assert value.__module__ == 'markdown_transaction'
    assert pickle.loads(pickle.dumps(value, protocol=4)) is value
