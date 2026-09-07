from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import markdown_transaction
import pytest
from markdown_transaction import (
    ABSENT,
    UNDO_RETENTION_DAYS,
    MarkdownChange,
    MarkdownCoordinator,
)
from project_journal import ProjectStore
from reliable_memory import OperationalDatabaseContractError, sha256_bytes


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    for relative in (
        "knowledge/daily",
        "knowledge/notes",
        "knowledge/projects/demo",
        "knowledge/inbox/claims",
    ):
        (root / relative).mkdir(parents=True)
    (root / "knowledge/index.md").write_bytes(b"index-v1\n")
    (root / "knowledge/log.md").write_bytes(b"log-v1\n")
    return root


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    return tmp_path / "state"


def _set_state(state_root: Path, transaction_id: str, state: str) -> None:
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        database.execute(
            'UPDATE "transaction" SET state = ? WHERE id = ?',
            (state, transaction_id),
        )
        database.commit()


def _row(state_root: Path, transaction_id: str) -> sqlite3.Row:
    database = sqlite3.connect(state_root / "run/markdown-transactions.sqlite3")
    database.row_factory = sqlite3.Row
    try:
        row = database.execute(
            'SELECT * FROM "transaction" WHERE id = ?', (transaction_id,)
        ).fetchone()
        assert row is not None
        return row
    finally:
        database.close()


def _crash_process(
    vault: Path, state_root: Path, killpoint: str, code: str, *arguments: str
) -> subprocess.CompletedProcess[str]:
    scripts = Path(__file__).parents[1] / "scripts"
    env = os.environ | {
        "LLM_WIKI_TRANSACTION_KILLPOINT": killpoint,
        "PYTHONPATH": str(scripts),
    }
    return subprocess.run(
        [sys.executable, "-c", code, str(vault), str(state_root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )


def _v2_parent(position: int) -> str | None:
    return "tx-preparing" if position else None


def _v2_error(state: str) -> str | None:
    if state in {"conflicted", "quarantined"}:
        return f"error-{state}"
    return None


def _v2_pruned_at(position: int, state: str) -> str | None:
    return f"pruned-{position}" if state == "committed" else None


def _v2_owner_pid(position: int, state: str) -> int | None:
    return 1000 + position if state == "preparing" else None


def _v2_transaction_row(position: int, state: str) -> tuple:
    """One legacy transaction row, built outside the test that inserts it."""
    return (
        f"tx-{state}",
        f"operation-{state}",
        f"request-{state}",
        state,
        f'{{"state":"{state}"}}',
        f"plan-{state}",
        f"created-{position}",
        f"updated-{position}",
        _v2_parent(position),
        _v2_error(state),
        _v2_pruned_at(position, state),
        _v2_owner_pid(position, state),
    )


def _v2_operation_row(position: int, kind: str, states: tuple) -> tuple:
    return (
        f"tx-{states[position]}",
        position,
        kind,
        f"knowledge/notes/{kind}.md",
        f"before-{kind}",
        f"after-{kind}",
        10 + position,
        20 + position,
        position % 2,
    )


def test_coordinator_v2_rows_survive_candidate_migration_exactly(
    vault: Path, state_root: Path
) -> None:
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction_columns = (
        "id",
        "operation_id",
        "request_hash",
        "state",
        "preconditions_json",
        "plan_hash",
        "created_at",
        "updated_at",
        "parent_transaction_id",
        "error_code",
        "artifacts_pruned_at",
        "owner_pid",
    )
    operation_columns = (
        "transaction_id",
        "position",
        "kind",
        "path",
        "before_hash",
        "after_hash",
        "parent_device",
        "parent_inode",
        "applied",
    )
    states = (
        "preparing",
        "prepared",
        "applying",
        "committed",
        "discarded",
        "conflicted",
        "quarantined",
    )
    with sqlite3.connect(coordinator.database_path) as database:
        for position, state in enumerate(states):
            database.execute(
                'INSERT INTO "transaction" '
                "(id, operation_id, request_hash, state, preconditions_json, plan_hash, "
                "created_at, updated_at, parent_transaction_id, error_code, "
                "artifacts_pruned_at, owner_pid) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _v2_transaction_row(position, state),
            )
        for position, kind in enumerate(("create", "replace", "delete")):
            database.execute(
                'INSERT INTO "operation" '
                "(transaction_id, position, kind, path, before_hash, after_hash, "
                "parent_device, parent_inode, applied) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _v2_operation_row(position, kind, states),
            )
        database.execute(
            "INSERT INTO project_checkpoints "
            "(project, sequence, occurrence_id, idempotency_key, event_json, "
            "lease_token, fencing_epoch, operation_id, attempt_number, "
            "parent_operation_id, transaction_id, state) "
            "VALUES ('demo', 1, 'occurrence', 'key', '{}', 'lease', 7, "
            "'checkpoint-operation', 1, NULL, 'tx-committed', 'committed')"
        )
        database.execute(
            "INSERT INTO project_checkpoint_attempts "
            "(project, sequence, attempt_number, operation_id, parent_operation_id, "
            "lease_token, fencing_epoch, transaction_id, state, created_at) "
            "VALUES ('demo', 1, 1, 'checkpoint-operation', NULL, 'lease', 7, "
            "'tx-committed', 'committed', 'checkpoint-created')"
        )
        database.execute(
            "INSERT INTO writer_fences(gate_name, last_epoch) VALUES ('global', 9)"
        )
        database.commit()
        expected_transactions = database.execute(
            f'SELECT {", ".join(transaction_columns)} FROM "transaction" ORDER BY id'
        ).fetchall()
        expected_operations = database.execute(
            f'SELECT {", ".join(operation_columns)} FROM "operation" '
            "ORDER BY transaction_id, position"
        ).fetchall()
        expected_checkpoints = database.execute(
            "SELECT * FROM project_checkpoints ORDER BY project, sequence"
        ).fetchall()
        expected_attempts = database.execute(
            "SELECT * FROM project_checkpoint_attempts "
            "ORDER BY project, sequence, attempt_number"
        ).fetchall()

    candidate = state_root / "run" / "markdown-transactions-v3.candidate.sqlite3"
    summary = markdown_transaction.initialize_coordinator_v3_candidate(
        candidate, source_v2=coordinator.database_path
    )

    with sqlite3.connect(candidate) as database:
        actual_transactions = database.execute(
            f'SELECT {", ".join(transaction_columns)} FROM "transaction" ORDER BY id'
        ).fetchall()
        new_fields = database.execute(
            "SELECT intent_id, intent_fence_token, intent_fence_epoch, "
            "capture_link_digest, capture_seal_digest, abort_operation_id, "
            "abort_manifest_sha256, abort_receipt_sha256, abort_chosen_at, aborted_at "
            'FROM "transaction"'
        ).fetchall()
        actual_operations = database.execute(
            f'SELECT {", ".join(operation_columns)} FROM "operation" '
            "ORDER BY transaction_id, position"
        ).fetchall()
        actual_checkpoints = database.execute(
            "SELECT * FROM project_checkpoints ORDER BY project, sequence"
        ).fetchall()
        actual_attempts = database.execute(
            "SELECT * FROM project_checkpoint_attempts "
            "ORDER BY project, sequence, attempt_number"
        ).fetchall()

    assert summary == {
        "operations": 3,
        "project_checkpoint_attempts": 1,
        "project_checkpoints": 1,
        "transactions": len(states),
        "writer_fences": 1,
    }
    assert actual_transactions == expected_transactions
    assert actual_operations == expected_operations
    assert actual_checkpoints == expected_checkpoints
    assert actual_attempts == expected_attempts
    assert all(row == (None,) * 10 for row in new_fields)


@pytest.mark.parametrize(
    ("table", "insert_sql"),
    [
        (
            "project_leases",
            "INSERT INTO project_leases VALUES "
            "('demo', 'token', 1, 'actor', '2099-01-01Z', '2026-08-12Z')",
        ),
        (
            "writer_owners",
            "INSERT INTO writer_owners VALUES "
            "('global', 'token', 1, 2, '2026-08-12Z', '2026-08-12Z', "
            "'2099-01-01Z', 1)",
        ),
        (
            "maintenance_owners",
            "INSERT INTO maintenance_owners VALUES "
            "('doctor', 'token', 1, '2026-08-12Z')",
        ),
    ],
)
def test_coordinator_migration_rejects_ambiguous_historical_owner(
    vault: Path, state_root: Path, table: str, insert_sql: str
) -> None:
    coordinator = MarkdownCoordinator(vault, state_root)
    with sqlite3.connect(coordinator.database_path) as database:
        database.execute(insert_sql)
        database.commit()
    candidate = state_root / "run" / f"candidate-{table}.sqlite3"

    with pytest.raises(OperationalDatabaseContractError) as raised:
        markdown_transaction.initialize_coordinator_v3_candidate(
            candidate, source_v2=coordinator.database_path
        )

    assert getattr(raised.value, "code", None) == "coordinator_v2_ambiguous_ownership"
    with sqlite3.connect(candidate) as database:
        assert database.execute("PRAGMA application_id").fetchone()[0] == 0
        assert database.execute("PRAGMA user_version").fetchone()[0] == 0


def test_coordinator_migration_rejects_incomplete_checkpoint_attempt_history(
    vault: Path, state_root: Path
) -> None:
    coordinator = MarkdownCoordinator(vault, state_root)
    with sqlite3.connect(coordinator.database_path) as database:
        database.execute(
            "INSERT INTO project_checkpoints "
            "(project, sequence, occurrence_id, idempotency_key, event_json, "
            "lease_token, fencing_epoch, operation_id, attempt_number, state) "
            "VALUES ('demo', 1, 'occurrence', 'key', '{}', 'lease', 1, "
            "'checkpoint-operation', 1, 'reserved')"
        )
        database.commit()
    candidate = state_root / "run" / "incomplete-checkpoint.sqlite3"

    with pytest.raises(OperationalDatabaseContractError) as raised:
        markdown_transaction.initialize_coordinator_v3_candidate(
            candidate, source_v2=coordinator.database_path
        )

    assert getattr(raised.value, "code", None) == (
        "coordinator_v2_checkpoint_history_incomplete"
    )


@pytest.mark.parametrize(
    "killpoint",
    [
        "after_preparing",
        "after_images_fsynced",
        "after_prepared",
        "after_applying",
        "after_each_target",
        "before_commit",
        "after_commit",
    ],
)
def test_forward_crash_boundaries_recover_to_known_state(
    vault: Path, state_root: Path, killpoint: str
):
    code = """
import sys
from pathlib import Path
from markdown_transaction import MarkdownChange, MarkdownCoordinator
c = MarkdownCoordinator(Path(sys.argv[1]), Path(sys.argv[2]))
tx = c.prepare([MarkdownChange.create('knowledge/notes/new.md', b'new')], operation_id='crash')
c.apply(tx.id)
"""
    crashed = _crash_process(vault, state_root, killpoint, code)
    assert crashed.returncode == 86, crashed.stderr

    recovered = MarkdownCoordinator(vault, state_root).recover()
    if recovered:
        assert recovered[0].state in {"committed", "discarded"}
    else:
        assert killpoint == "after_commit"
    expected = None if killpoint in {"after_preparing", "after_images_fsynced"} else b"new"
    target = vault / "knowledge/notes/new.md"
    assert (target.read_bytes() if target.exists() else None) == expected


@pytest.mark.parametrize(
    "killpoint",
    [
        "after_undo_preparing",
        "after_undo_images_fsynced",
        "after_undo_prepared",
        "after_undo_applying",
        "after_each_undo_target",
        "before_undo_commit",
        "after_undo_commit",
    ],
)
def test_undo_crash_boundaries_recover_to_known_state(
    vault: Path, state_root: Path, killpoint: str
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    original = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id=f"undo-source:{killpoint}",
    )
    coordinator.apply(original.id)
    code = """
import sys
from pathlib import Path
from markdown_transaction import MarkdownCoordinator
c = MarkdownCoordinator(Path(sys.argv[1]), Path(sys.argv[2]))
undo = c.undo(sys.argv[3])
c.apply(undo.id)
"""
    crashed = _crash_process(vault, state_root, killpoint, code, original.id)
    assert crashed.returncode == 86, crashed.stderr

    recovered = MarkdownCoordinator(vault, state_root).recover()
    if recovered:
        assert recovered[0].state in {"committed", "discarded"}
    else:
        assert killpoint == "after_undo_commit"
    expected = (
        b"after"
        if killpoint in {"after_undo_preparing", "after_undo_images_fsynced"}
        else b"before"
    )
    assert target.read_bytes() == expected


def test_dead_preparing_with_complete_fsynced_plan_rolls_forward(
    vault: Path, state_root: Path
):
    code = """
import sys
from pathlib import Path
from markdown_transaction import MarkdownChange, MarkdownCoordinator
c = MarkdownCoordinator(Path(sys.argv[1]), Path(sys.argv[2]))
c.prepare([MarkdownChange.create('knowledge/notes/new.md', b'new')], operation_id='durable-plan')
"""
    crashed = _crash_process(vault, state_root, "after_plan_fsynced", code)
    assert crashed.returncode == 86, crashed.stderr

    recovered = MarkdownCoordinator(vault, state_root).recover()

    assert [(record.operation_id, record.state) for record in recovered] == [
        ("durable-plan", "committed")
    ]
    assert (vault / "knowledge/notes/new.md").read_bytes() == b"new"


@pytest.mark.parametrize(
    ("kind", "before", "after"),
    [
        ("create", None, b"external journal bytes"),
        ("replace", b"before journal bytes", b"external replacement bytes"),
        ("delete", b"before journal bytes", None),
    ],
)
def test_dead_preparing_never_owns_ambiguous_matching_after_bytes(
    vault: Path,
    state_root: Path,
    kind: str,
    before: bytes | None,
    after: bytes | None,
):
    target = vault / "knowledge/projects/demo/journal.md"
    if before is not None:
        target.write_bytes(before)
    expires = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat().replace(
        "+00:00", "Z"
    )
    MarkdownCoordinator(vault, state_root)
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        database.execute(
            "INSERT INTO project_leases VALUES (?, ?, ?, ?, ?, ?)",
            ("demo", "token", 1, "agent", expires, expires),
        )
        database.commit()
    code = """
import sys
from pathlib import Path
from markdown_transaction import MarkdownChange, MarkdownCoordinator
vault, state, kind, expires = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
path = 'knowledge/projects/demo/journal.md'
change = {
    'create': MarkdownChange.create(path, b'external journal bytes'),
    'replace': MarkdownChange.replace(path, b'external replacement bytes'),
    'delete': MarkdownChange.delete(path),
}[kind]
c = MarkdownCoordinator(vault, state)
c.prepare([change], operation_id=f'ambiguous:{kind}', preconditions={
    'project_lease': {
        'project': 'demo', 'lease_token': 'token', 'fencing_epoch': 1,
        'expires_at': expires,
    }
})
"""
    crashed = _crash_process(
        vault, state_root, "after_plan_fsynced", code, kind, expires
    )
    assert crashed.returncode == 86, crashed.stderr
    if after is None:
        target.unlink()
    else:
        target.write_bytes(after)
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        database.execute(
            "UPDATE project_leases SET lease_token = 'new-token', fencing_epoch = 2 "
            "WHERE project = 'demo'"
        )
        database.commit()

    recovered = MarkdownCoordinator(vault, state_root).recover()[0]

    assert (recovered.state, recovered.error_code) == (
        "quarantined",
        "precondition_failed",
    )
    assert (target.read_bytes() if target.exists() else None) == after
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        assert database.execute(
            'SELECT applied FROM "operation" WHERE transaction_id = ?',
            (recovered.id,),
        ).fetchone()[0] == 0


def test_dead_preparing_matching_after_rolls_forward_with_valid_precondition(
    vault: Path, state_root: Path
):
    expires = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat().replace(
        "+00:00", "Z"
    )
    MarkdownCoordinator(vault, state_root)
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        database.execute(
            "INSERT INTO project_leases VALUES (?, ?, ?, ?, ?, ?)",
            ("demo", "token", 1, "agent", expires, expires),
        )
        database.commit()
    code = """
import sys
from pathlib import Path
from markdown_transaction import MarkdownChange, MarkdownCoordinator
c = MarkdownCoordinator(Path(sys.argv[1]), Path(sys.argv[2]))
c.prepare(
    [MarkdownChange.create('knowledge/projects/demo/journal.md', b'journal bytes')],
    operation_id='valid-ambiguous',
    preconditions={'project_lease': {
        'project': 'demo', 'lease_token': 'token', 'fencing_epoch': 1,
        'expires_at': sys.argv[3],
    }},
)
"""
    crashed = _crash_process(
        vault, state_root, "after_plan_fsynced", code, expires
    )
    assert crashed.returncode == 86, crashed.stderr
    target = vault / "knowledge/projects/demo/journal.md"
    target.write_bytes(b"journal bytes")

    recovered = MarkdownCoordinator(vault, state_root).recover()[0]

    assert recovered.state == "committed"
    assert target.read_bytes() == b"journal bytes"


def test_project_recovery_promotion_binds_reserved_checkpoint(
    vault: Path, state_root: Path
):
    event = {
        "schema_version": "project-checkpoint/v1",
        "occurrence_id": "evt-crash",
        "idempotency_key": "checkpoint:crash",
        "provenance": {
            "agent": "agent-a",
            "session": "session-1",
            "worktree": "D:/work/wiki",
            "branch": "feature/journal",
            "source_event": "event-1",
        },
        "trigger": "task_completed",
        "reason": "crash recovery",
        "delta": {
            "goal": {"id": "g", "action": "upsert", "value": "Recover"},
            "phase": {"id": "p", "action": "upsert", "value": "Test"},
            "current_task": {"id": "t", "action": "upsert", "value": "Replay"},
            "next_actions": [],
            "decisions": [],
            "blockers": [],
            "changed_files": [],
            "commands": [],
            "verification": [],
        },
        "evidence_event_ids": ["event-1"],
    }
    code = """
import json, sys
from pathlib import Path
from project_journal import ProjectStore
store = ProjectStore(Path(sys.argv[1]), Path(sys.argv[2]))
store.checkpoint('demo', json.loads(sys.argv[3]), 'agent-a')
"""

    crashed = _crash_process(
        vault,
        state_root,
        "after_plan_fsynced",
        code,
        json.dumps(event, separators=(",", ":")),
    )
    assert crashed.returncode == 86, crashed.stderr

    recovered = ProjectStore(vault, state_root).recover("demo")

    assert [receipt.sequence for receipt in recovered] == [1]
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        row = database.execute(
            "SELECT state, transaction_id FROM project_checkpoints WHERE project = 'demo'"
        ).fetchone()
    assert row[0] == "committed"
    assert row[1]
    assert (vault / "knowledge/projects/demo/journal.md").exists()
    assert (vault / "knowledge/projects/demo/state.md").exists()


def test_complete_dead_preparing_with_unknown_target_conflicts_instead_of_discarding(
    vault: Path, state_root: Path
):
    code = """
import sys
from pathlib import Path
from markdown_transaction import MarkdownChange, MarkdownCoordinator
c = MarkdownCoordinator(Path(sys.argv[1]), Path(sys.argv[2]))
c.prepare([MarkdownChange.create('knowledge/notes/new.md', b'new')], operation_id='durable-conflict')
"""
    crashed = _crash_process(vault, state_root, "after_plan_fsynced", code)
    assert crashed.returncode == 86, crashed.stderr
    target = vault / "knowledge/notes/new.md"
    target.write_bytes(b"unknown")

    recovered = MarkdownCoordinator(vault, state_root).recover()[0]

    assert (recovered.state, recovered.error_code) == (
        "conflicted",
        "before_hash_mismatch",
    )
    assert target.read_bytes() == b"unknown"


def test_dead_preparing_with_malformed_manifest_is_discarded(
    vault: Path, state_root: Path
):
    code = """
import sys
from pathlib import Path
from markdown_transaction import MarkdownChange, MarkdownCoordinator
c = MarkdownCoordinator(Path(sys.argv[1]), Path(sys.argv[2]))
c.prepare([MarkdownChange.create('knowledge/notes/new.md', b'new')], operation_id='bad-manifest')
"""
    crashed = _crash_process(vault, state_root, "after_plan_fsynced", code)
    assert crashed.returncode == 86, crashed.stderr
    transaction_root = state_root / "run/transactions"
    artifact_root = next(transaction_root.iterdir())
    manifest_path = artifact_root / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["operations"][0]["parent_device"] = "not-an-integer"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    recovered = MarkdownCoordinator(vault, state_root).recover()[0]

    assert recovered.state == "discarded"
    assert not (vault / "knowledge/notes/new.md").exists()


def test_recovery_promotion_persists_unsigned_64_bit_parent_identity(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    code = """
import sys
from pathlib import Path
from markdown_transaction import MarkdownChange, MarkdownCoordinator
c = MarkdownCoordinator(Path(sys.argv[1]), Path(sys.argv[2]))
c.prepare([MarkdownChange.create('knowledge/notes/new.md', b'new')], operation_id='unsigned-recovery')
"""
    crashed = _crash_process(vault, state_root, "after_plan_fsynced", code)
    assert crashed.returncode == 86, crashed.stderr
    manifest_path = next((state_root / "run/transactions").iterdir()) / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    parent_identity = (11853635609087352826, (1 << 63) + 7)
    manifest["operations"][0]["parent_device"] = parent_identity[0]
    manifest["operations"][0]["parent_inode"] = parent_identity[1]
    manifest_path.write_bytes(markdown_transaction.canonical_json_bytes(manifest))
    coordinator = MarkdownCoordinator(vault, state_root)
    monkeypatch.setattr(coordinator, "_parent_identity", lambda path: parent_identity)

    promotion = coordinator._promote_preparing(
        coordinator._record(manifest["transaction_id"])
    )

    assert promotion == "promoted"
    with sqlite3.connect(coordinator.database_path) as database:
        persisted = database.execute(
            'SELECT parent_device, parent_inode FROM "operation" WHERE transaction_id = ?',
            (manifest["transaction_id"],),
        ).fetchone()
    assert tuple(markdown_transaction._decode_filesystem_id(value) for value in persisted) == (
        parent_identity
    )

@pytest.mark.parametrize("state", ["prepared", "applying"])
def test_recover_rolls_forward_prepared_and_applying(
    vault: Path, state_root: Path, state: str
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id=f"roll-forward:{state}",
    )
    _set_state(state_root, transaction.id, state)

    recovered = coordinator.recover()

    assert [(record.id, record.state) for record in recovered] == [
        (transaction.id, "committed")
    ]
    assert target.read_bytes() == b"after"


def test_recover_treats_after_hash_as_idempotent(vault: Path, state_root: Path):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="idempotent-after",
    )
    target.write_bytes(b"after")

    assert coordinator.recover()[0].state == "committed"
    assert target.read_bytes() == b"after"


def test_recover_conflicts_when_target_matches_neither_hash(
    vault: Path, state_root: Path
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="unknown-bytes",
    )
    target.write_bytes(b"private unknown content")

    recovered = coordinator.recover()[0]

    assert recovered.state == "conflicted"
    assert recovered.error_code == "unknown_target_bytes"
    assert target.read_bytes() == b"private unknown content"
    assert "private unknown content" not in json.dumps(coordinator.deletion_blockers())


def test_recovery_hashes_oversized_changed_target_without_materializing_it(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    target = vault / "knowledge/guardrails.md"
    target.write_bytes(b"before")
    monkeypatch.setattr(markdown_transaction, "MAX_KNOWLEDGE_TARGET_BYTES", 8)
    coordinator = MarkdownCoordinator(vault, state_root)
    coordinator.prepare(
        [MarkdownChange.replace("knowledge/guardrails.md", b"after")],
        operation_id="oversized-before-recovery",
    )
    target.write_bytes(b"x" * 9)
    real_sha256_bytes = markdown_transaction.sha256_bytes

    def reject_materialized_oversize(value: bytes) -> str:
        assert value != b"x" * 9, "oversized target was materialized for hashing"
        return real_sha256_bytes(value)

    monkeypatch.setattr(markdown_transaction, "sha256_bytes", reject_materialized_oversize)

    recovered = coordinator.recover()[0]

    assert (recovered.state, recovered.error_code) == (
        "conflicted",
        "unknown_target_bytes",
    )
    assert target.read_bytes() == b"x" * 9


def test_recover_create_conflicts_if_target_appeared(vault: Path, state_root: Path):
    target = vault / "knowledge/notes/new.md"
    coordinator = MarkdownCoordinator(vault, state_root)
    coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/new.md", b"transaction")],
        operation_id="appeared",
    )
    target.write_bytes(b"external")

    recovered = coordinator.recover()[0]

    assert recovered.state == "conflicted"
    assert recovered.error_code == "before_hash_mismatch"
    assert target.read_bytes() == b"external"


def test_recover_delete_only_deletes_recorded_before_hash(
    vault: Path, state_root: Path
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    good = coordinator.prepare(
        [MarkdownChange.delete("knowledge/notes/page.md")], operation_id="delete-good"
    )
    assert coordinator.recover()[0].state == "committed"
    assert not target.exists()

    target.write_bytes(b"second")
    bad = coordinator.prepare(
        [MarkdownChange.delete("knowledge/notes/page.md")], operation_id="delete-bad"
    )
    target.write_bytes(b"external")
    recovered = coordinator.recover()[0]
    assert recovered.id == bad.id
    assert recovered.state == "conflicted"
    assert target.read_bytes() == b"external"
    assert good.id != bad.id


def test_invalid_preparing_is_discarded_without_target_writes(
    vault: Path, state_root: Path
):
    coordinator = MarkdownCoordinator(vault, state_root)
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        database.execute(
            'INSERT INTO "transaction" '
            "(id, operation_id, request_hash, state, preconditions_json, plan_hash, "
            "created_at, updated_at) VALUES (?, ?, ?, 'preparing', '{}', '', ?, ?)",
            ("incomplete", "incomplete-op", "request", timestamp, timestamp),
        )
        database.commit()

    recovered = coordinator.recover()

    assert recovered[0].state == "discarded"
    assert not (vault / "knowledge/notes/new.md").exists()


def test_recovery_does_not_discard_a_live_preparer(vault: Path, state_root: Path):
    coordinator = MarkdownCoordinator(vault, state_root)
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        database.execute(
            'INSERT INTO "transaction" '
            "(id, operation_id, request_hash, state, preconditions_json, plan_hash, "
            "created_at, updated_at, owner_pid) "
            "VALUES (?, ?, ?, 'preparing', '{}', '', ?, ?, ?)",
            ("live", "live-op", "request", timestamp, timestamp, os.getpid()),
        )
        database.commit()

    assert coordinator.recover() == []
    assert _row(state_root, "live")["state"] == "preparing"


def test_corrupt_after_image_does_not_rollback_ambiguous_after_hash_without_receipt(
    vault: Path, state_root: Path
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="corrupt-after",
    )
    target.write_bytes(b"after")
    artifact = state_root / "run/transactions" / transaction.id / "after/000000.bin"
    artifact.write_bytes(b"corrupt")

    recovered = coordinator.recover()[0]

    assert recovered.state == "quarantined"
    assert recovered.error_code == "after_image_corrupt"
    assert target.read_bytes() == b"after"
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        assert database.execute(
            'SELECT applied FROM "operation" WHERE transaction_id = ?',
            (transaction.id,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("kind", "before", "after"),
    [
        ("create", None, b"created externally"),
        ("replace", b"before", b"replaced externally"),
        ("delete", b"before", None),
    ],
)
def test_corrupt_other_image_preserves_ambiguous_applied_zero_target(
    vault: Path,
    state_root: Path,
    kind: str,
    before: bytes | None,
    after: bytes | None,
):
    target = vault / "knowledge/notes/page.md"
    if before is not None:
        target.write_bytes(before)
    changes = {
        "create": MarkdownChange.create(
            "knowledge/notes/page.md", b"created externally"
        ),
        "replace": MarkdownChange.replace(
            "knowledge/notes/page.md", b"replaced externally"
        ),
        "delete": MarkdownChange.delete("knowledge/notes/page.md"),
    }
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [
            changes[kind],
            MarkdownChange.create("knowledge/notes/corrupt-source.md", b"source"),
        ],
        operation_id=f"corrupt-ambiguous:{kind}",
    )
    if after is None:
        target.unlink()
    else:
        target.write_bytes(after)
    artifact = state_root / "run/transactions" / transaction.id / "after/000001.bin"
    artifact.write_bytes(b"corrupt")

    recovered = coordinator.recover()[0]

    assert (recovered.state, recovered.error_code) == (
        "quarantined",
        "after_image_corrupt",
    )
    assert (target.read_bytes() if target.exists() else None) == after
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        assert database.execute(
            'SELECT applied FROM "operation" WHERE transaction_id = ? AND position = 0',
            (transaction.id,),
        ).fetchone()[0] == 0


def test_corrupt_after_image_rolls_back_durably_applied_target(
    vault: Path, state_root: Path
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="corrupt-applied",
    )
    target.write_bytes(b"after")
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        database.execute(
            'UPDATE "operation" SET applied = 1 WHERE transaction_id = ?',
            (transaction.id,),
        )
        database.commit()
    artifact = state_root / "run/transactions" / transaction.id / "after/000000.bin"
    artifact.write_bytes(b"corrupt")

    recovered = coordinator.recover()[0]

    assert (recovered.state, recovered.error_code) == (
        "discarded",
        "after_image_corrupt",
    )
    assert target.read_bytes() == b"before"


def test_corrupt_after_image_quarantines_unknown_target_bytes(
    vault: Path, state_root: Path
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="corrupt-unknown",
    )
    target.write_bytes(b"unknown")
    artifact = state_root / "run/transactions" / transaction.id / "after/000000.bin"
    artifact.write_bytes(b"corrupt")

    recovered = coordinator.recover()[0]

    assert recovered.state == "quarantined"
    assert recovered.error_code == "after_image_corrupt"
    assert target.read_bytes() == b"unknown"


def test_apply_parent_identity_change_quarantines_and_releases_gate(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="parent-change-apply",
    )
    parent_identity = coordinator._parent_identity
    monkeypatch.setattr(
        coordinator,
        "_parent_identity",
        lambda path: (
            parent_identity(path)[0],
            parent_identity(path)[1] + 1,
        ),
    )

    with pytest.raises(RuntimeError, match="parent identity"):
        coordinator.apply(transaction.id)

    record = coordinator._record(transaction.id)
    assert (record.state, record.error_code) == (
        "quarantined",
        "parent_identity_changed",
    )
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        assert database.execute("SELECT * FROM writer_owners").fetchall() == []
    monkeypatch.setattr(coordinator, "_parent_identity", parent_identity)
    unrelated = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/unrelated.md", b"unrelated")],
        operation_id="after-parent-change",
    )
    assert unrelated.state == "prepared"


def test_recover_reparse_change_quarantines_and_does_not_block_prepare(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="reparse-recovery",
    )
    monkeypatch.setattr(
        markdown_transaction,
        "_is_reparse_point",
        lambda path: path == vault / "knowledge/notes",
    )

    recovered = coordinator.recover()

    assert [(record.id, record.state, record.error_code) for record in recovered] == [
        (transaction.id, "quarantined", "parent_identity_changed")
    ]
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        assert database.execute("SELECT * FROM writer_owners").fetchall() == []
    unrelated = coordinator.prepare(
        [MarkdownChange.replace("knowledge/index.md", b"unrelated")],
        operation_id="after-reparse-change",
    )
    assert unrelated.state == "prepared"


def test_project_lease_precondition_is_persisted_and_rechecked_before_apply(
    vault: Path, state_root: Path
):
    coordinator = MarkdownCoordinator(vault, state_root)
    expires = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat().replace(
        "+00:00", "Z"
    )
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        database.execute(
            "INSERT INTO project_leases VALUES (?, ?, ?, ?, ?, ?)",
            ("demo", "new-token", 2, "agent", expires, expires),
        )
        database.commit()
    transaction = coordinator.prepare(
        [
            MarkdownChange.create("knowledge/projects/demo/journal.md", b"event"),
            MarkdownChange.replace("knowledge/index.md", b"projection"),
        ],
        operation_id="stale-checkpoint",
        preconditions={
            "project_lease": {
                "project": "demo",
                "lease_token": "old-token",
                "fencing_epoch": 1,
                "expires_at": expires,
            }
        },
    )

    recovered = coordinator.recover()[0]

    assert recovered.id == transaction.id
    assert recovered.state == "quarantined"
    assert recovered.error_code == "precondition_failed"
    assert not (vault / "knowledge/projects/demo/journal.md").exists()
    assert (vault / "knowledge/index.md").read_bytes() == b"index-v1\n"


def test_stale_prepared_transaction_does_not_rollback_external_after_hash_bytes(
    vault: Path, state_root: Path
):
    coordinator = MarkdownCoordinator(vault, state_root)
    expires = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat().replace(
        "+00:00", "Z"
    )
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        database.execute(
            "INSERT INTO project_leases VALUES (?, ?, ?, ?, ?, ?)",
            ("demo", "token", 1, "agent", expires, expires),
        )
        database.commit()
    transaction = coordinator.prepare(
        [MarkdownChange.create("knowledge/projects/demo/journal.md", b"same-bytes")],
        operation_id="stale-before-apply",
        preconditions={
            "project_lease": {
                "project": "demo",
                "lease_token": "token",
                "fencing_epoch": 1,
                "expires_at": expires,
            }
        },
    )
    journal = vault / "knowledge/projects/demo/journal.md"
    journal.write_bytes(b"same-bytes")
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        database.execute(
            "UPDATE project_leases SET lease_token = 'new-token', fencing_epoch = 2 "
            "WHERE project = 'demo'"
        )
        database.commit()

    recovered = coordinator.recover()[0]

    assert recovered.state == "quarantined"
    assert journal.read_bytes() == b"same-bytes"
    assert coordinator._record(transaction.id).error_code == "precondition_failed"


def test_unrelated_guard_fails_before_ambiguous_after_state_is_inferred_applied(
    vault: Path, state_root: Path
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    guard = vault / "knowledge/index.md"
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"ambiguous-after")],
        operation_id="unrelated-guard-before-reconcile",
        preconditions={
            "knowledge/notes/page.md": sha256_bytes(b"before"),
            "knowledge/index.md": sha256_bytes(guard.read_bytes()),
        },
    )
    target.write_bytes(b"ambiguous-after")
    guard.write_bytes(b"changed-guard")

    recovered = coordinator.recover()[0]

    assert (recovered.state, recovered.error_code) == (
        "quarantined",
        "precondition_failed",
    )
    assert target.read_bytes() == b"ambiguous-after"
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        assert database.execute(
            'SELECT applied FROM "operation" WHERE transaction_id = ?',
            (transaction.id,),
        ).fetchone()[0] == 0


def test_same_target_precondition_accepts_after_state_for_crash_reconciliation(
    vault: Path, state_root: Path
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="same-target-crash",
        preconditions={"knowledge/notes/page.md": sha256_bytes(b"before")},
    )
    _set_state(state_root, transaction.id, "applying")
    target.write_bytes(b"after")

    recovered = coordinator.recover()[0]

    assert recovered.state == "committed"
    assert target.read_bytes() == b"after"
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        assert database.execute(
            'SELECT applied FROM "operation" WHERE transaction_id = ?',
            (transaction.id,),
        ).fetchone()[0] == 1


@pytest.mark.parametrize("takeover_after_call", [1, 2])
def test_takeover_during_partial_checkpoint_is_blocked_until_commit(
    vault: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    takeover_after_call: int,
):
    journal = vault / "knowledge/projects/demo/journal.md"
    journal.write_bytes(b"old-event\n")
    projection = vault / "knowledge/index.md"
    coordinator = MarkdownCoordinator(vault, state_root)
    expires = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat().replace(
        "+00:00", "Z"
    )
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        database.execute(
            "INSERT INTO project_leases VALUES (?, ?, ?, ?, ?, ?)",
            ("demo", "token", 1, "agent", expires, expires),
        )
        database.commit()
    transaction = coordinator.prepare(
        [
            MarkdownChange.replace(
                "knowledge/projects/demo/journal.md", b"old-event\nnew-event\n"
            ),
            MarkdownChange.replace("knowledge/index.md", b"projected-new-event\n"),
        ],
        operation_id="partial-checkpoint",
        preconditions={
            "project_lease": {
                "project": "demo",
                "lease_token": "token",
                "fencing_epoch": 1,
                "expires_at": expires,
            }
        },
    )
    mutate = coordinator._mutate_and_mark
    calls = 0

    blocked = 0

    def attempt_takeover(*args, **kwargs):
        nonlocal blocked
        nonlocal calls
        mutate(*args, **kwargs)
        calls += 1
        if calls == takeover_after_call:
            with sqlite3.connect(
                state_root / "run/markdown-transactions.sqlite3", timeout=0.1
            ) as database:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    database.execute(
                        "UPDATE project_leases SET lease_token = 'new-token', "
                        "fencing_epoch = 2 WHERE project = 'demo'"
                    )
                    database.commit()
                blocked += 1

    monkeypatch.setattr(coordinator, "_mutate_and_mark", attempt_takeover)

    committed = coordinator.apply(transaction.id)

    assert committed.state == "committed"
    assert blocked == 1
    assert journal.read_bytes() == b"old-event\nnew-event\n"
    assert projection.read_bytes() == b"projected-new-event\n"


def test_project_takeover_is_locked_out_from_final_check_through_commit(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    coordinator = MarkdownCoordinator(vault, state_root)
    expires = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat().replace(
        "+00:00", "Z"
    )
    with sqlite3.connect(coordinator.database_path) as database:
        database.execute(
            "INSERT INTO project_leases VALUES (?, ?, ?, ?, ?, ?)",
            ("demo", "old-token", 1, "agent-a", expires, expires),
        )
        database.commit()
    transaction = coordinator.prepare(
        [MarkdownChange.create("knowledge/projects/demo/journal.md", b"event\n")],
        operation_id="project-final-fence",
        preconditions={
            "project_lease": {
                "project": "demo",
                "lease_token": "old-token",
                "fencing_epoch": 1,
                "expires_at": expires,
            }
        },
    )
    entered = threading.Event()
    release = threading.Event()
    mutate = coordinator._mutate_and_mark

    def pause_before_mutation(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return mutate(*args, **kwargs)

    monkeypatch.setattr(coordinator, "_mutate_and_mark", pause_before_mutation)
    with ThreadPoolExecutor(max_workers=1) as pool:
        applying = pool.submit(coordinator.apply, transaction.id)
        assert entered.wait(5)
        with sqlite3.connect(coordinator.database_path, timeout=0.1) as contender:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute(
                    "UPDATE project_leases SET lease_token = 'new-token', "
                    "fencing_epoch = 2 WHERE project = 'demo'"
                )
                contender.commit()
        release.set()
        assert applying.result(timeout=5).state == "committed"

    with sqlite3.connect(coordinator.database_path) as database:
        database.execute(
            "UPDATE project_leases SET lease_token = 'new-token', fencing_epoch = 2 "
            "WHERE project = 'demo'"
        )
        database.commit()
    assert (vault / "knowledge/projects/demo/journal.md").read_bytes() == b"event\n"


def test_new_project_epoch_wins_before_final_fence_and_old_touches_nothing(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    coordinator = MarkdownCoordinator(vault, state_root)
    expires = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat().replace(
        "+00:00", "Z"
    )
    with sqlite3.connect(coordinator.database_path) as database:
        database.execute(
            "INSERT INTO project_leases VALUES (?, ?, ?, ?, ?, ?)",
            ("demo", "old-token", 1, "agent-a", expires, expires),
        )
        database.commit()
    transaction = coordinator.prepare(
        [MarkdownChange.create("knowledge/projects/demo/journal.md", b"event\n")],
        operation_id="project-new-epoch-wins",
        preconditions={
            "project_lease": {
                "project": "demo",
                "lease_token": "old-token",
                "fencing_epoch": 1,
                "expires_at": expires,
            }
        },
    )
    early_check_done = threading.Event()
    takeover_done = threading.Event()
    check = coordinator._check_preconditions
    paused = False

    def pause_after_early_check(*args, **kwargs):
        nonlocal paused
        result = check(*args, **kwargs)
        if kwargs.get("database") is None and not paused:
            paused = True
            early_check_done.set()
            assert takeover_done.wait(5)
        return result

    monkeypatch.setattr(coordinator, "_check_preconditions", pause_after_early_check)
    with ThreadPoolExecutor(max_workers=1) as pool:
        applying = pool.submit(coordinator.apply, transaction.id)
        assert early_check_done.wait(5)
        with sqlite3.connect(coordinator.database_path) as database:
            database.execute(
                "UPDATE project_leases SET lease_token = 'new-token', "
                "fencing_epoch = 2 WHERE project = 'demo'"
            )
            database.commit()
        takeover_done.set()
        with pytest.raises(RuntimeError, match="precondition"):
            applying.result(timeout=5)

    assert not (vault / "knowledge/projects/demo/journal.md").exists()
    assert coordinator._record(transaction.id).state == "quarantined"


def test_project_lease_expiry_during_apply_restores_all_targets(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    coordinator = MarkdownCoordinator(vault, state_root)
    journal = vault / "knowledge/projects/demo/journal.md"
    journal.write_bytes(b"old-event\n")
    expires = (datetime.now(timezone.utc) + timedelta(seconds=0.5)).isoformat().replace(
        "+00:00", "Z"
    )
    with sqlite3.connect(coordinator.database_path) as database:
        database.execute(
            "INSERT INTO project_leases VALUES (?, ?, ?, ?, ?, ?)",
            ("demo", "token", 1, "agent", expires, expires),
        )
        database.commit()
    transaction = coordinator.prepare(
        [
            MarkdownChange.replace(
                "knowledge/projects/demo/journal.md", b"new-event\n"
            ),
            MarkdownChange.replace("knowledge/index.md", b"projected\n"),
        ],
        operation_id="project-expiry-during-apply",
        preconditions={
            "project_lease": {
                "project": "demo",
                "lease_token": "token",
                "fencing_epoch": 1,
                "expires_at": expires,
            }
        },
    )
    before_mutation = coordinator._before_target_mutation
    delayed = False

    def expire_during_first_mutation(target: Path):
        nonlocal delayed
        before_mutation(target)
        if not delayed:
            delayed = True
            time.sleep(0.6)

    monkeypatch.setattr(
        coordinator, "_before_target_mutation", expire_during_first_mutation
    )

    with pytest.raises(RuntimeError, match="precondition"):
        coordinator.apply(transaction.id)

    assert journal.read_bytes() == b"old-event\n"
    assert (vault / "knowledge/index.md").read_bytes() == b"index-v1\n"
    assert coordinator._record(transaction.id).state == "quarantined"


def test_partial_checkpoint_rollback_leaves_unknown_journal_bytes_untouched(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    journal = vault / "knowledge/projects/demo/journal.md"
    journal.write_bytes(b"old-event\n")
    coordinator = MarkdownCoordinator(vault, state_root)
    expires = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat().replace(
        "+00:00", "Z"
    )
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        database.execute(
            "INSERT INTO project_leases VALUES (?, ?, ?, ?, ?, ?)",
            ("demo", "token", 1, "agent", expires, expires),
        )
        database.commit()
    transaction = coordinator.prepare(
        [
            MarkdownChange.replace(
                "knowledge/projects/demo/journal.md", b"old-event\nnew-event\n"
            ),
            MarkdownChange.replace("knowledge/index.md", b"projection\n"),
        ],
        operation_id="partial-checkpoint-unknown",
        preconditions={
            "project_lease": {
                "project": "demo",
                "lease_token": "token",
                "fencing_epoch": 1,
                "expires_at": expires,
            }
        },
    )
    mutate = coordinator._mutate_and_mark
    calls = 0

    def external_edit_after_journal(*args, **kwargs):
        nonlocal calls
        mutate(*args, **kwargs)
        calls += 1
        if calls == 1:
            journal.write_bytes(b"private unknown journal bytes")

    monkeypatch.setattr(coordinator, "_mutate_and_mark", external_edit_after_journal)

    with pytest.raises(RuntimeError, match="after state mismatch"):
        coordinator.apply(transaction.id)

    assert coordinator._record(transaction.id).state == "conflicted"
    assert journal.read_bytes() == b"private unknown journal bytes"
    assert (vault / "knowledge/index.md").read_bytes() == b"index-v1\n"


def test_prepare_recovers_pending_transactions_before_snapshot(
    vault: Path, state_root: Path
):
    coordinator = MarkdownCoordinator(vault, state_root)
    first = coordinator.prepare(
        [MarkdownChange.replace("knowledge/index.md", b"index-v2")],
        operation_id="first",
    )

    second = coordinator.prepare(
        [MarkdownChange.replace("knowledge/index.md", b"index-v3")],
        operation_id="second",
    )

    assert _row(state_root, first.id)["state"] == "committed"
    assert second.operations[0].before_hash == sha256_bytes(b"index-v2")


def test_same_operation_id_retry_returns_recovered_committed_record(
    vault: Path, state_root: Path
):
    coordinator = MarkdownCoordinator(vault, state_root)
    changes = [MarkdownChange.create("knowledge/notes/new.md", b"new")]
    first = coordinator.prepare(changes, operation_id="same-recovery")

    retried = coordinator.prepare(changes, operation_id="same-recovery")

    assert retried.id == first.id
    assert retried.state == "committed"
    assert (vault / "knowledge/notes/new.md").read_bytes() == b"new"


def test_same_operation_id_retry_returns_recovered_conflict(
    vault: Path, state_root: Path
):
    target = vault / "knowledge/notes/new.md"
    coordinator = MarkdownCoordinator(vault, state_root)
    changes = [MarkdownChange.create("knowledge/notes/new.md", b"new")]
    first = coordinator.prepare(changes, operation_id="same-conflict")
    target.write_bytes(b"unknown")

    retried = coordinator.prepare(changes, operation_id="same-conflict")

    assert retried.id == first.id
    assert (retried.state, retried.error_code) == (
        "conflicted",
        "before_hash_mismatch",
    )
    assert target.read_bytes() == b"unknown"


def test_settle_rereads_terminal_state_after_concurrent_apply(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = MarkdownCoordinator(vault, state_root)
    second = MarkdownCoordinator(vault, state_root)
    transaction = first.prepare(
        [MarkdownChange.replace("knowledge/index.md", b"index-v2\n")],
        operation_id="concurrent-settle-conflict",
    )
    (vault / "knowledge/index.md").write_bytes(b"external-change\n")
    applying = threading.Event()
    release = threading.Event()
    original = first._check_preconditions

    def pause_before_conflict(*args, **kwargs):
        applying.set()
        assert release.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(first, "_check_preconditions", pause_before_conflict)
    with ThreadPoolExecutor(max_workers=2) as pool:
        conflicting = pool.submit(first.apply, transaction.id)
        assert applying.wait(timeout=10)
        settling = pool.submit(
            markdown_transaction._settle_operation,
            second,
            transaction.operation_id,
        )
        time.sleep(0.05)
        release.set()
        with pytest.raises(markdown_transaction.TransactionFailure):
            conflicting.result(timeout=10)
        settled = settling.result(timeout=10)

    assert settled is not None
    assert (settled.state, settled.error_code) == (
        "conflicted",
        "before_hash_mismatch",
    )


def test_concurrent_recovery_is_safe(vault: Path, state_root: Path):
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/new.md", b"new")],
        operation_id="concurrent-recovery",
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: MarkdownCoordinator(vault, state_root).recover(),
                range(2),
            )
        )

    assert all(not result or result[0].state == "committed" for result in results)
    assert _row(state_root, transaction.id)["state"] == "committed"
    assert (vault / "knowledge/notes/new.md").read_bytes() == b"new"


def test_recovery_honors_transaction_limit_and_deadline(vault: Path, state_root: Path):
    coordinator = MarkdownCoordinator(vault, state_root)
    transactions = [
        coordinator.prepare(
            [MarkdownChange.create(f"knowledge/notes/{number}.md", b"new")],
            operation_id=f"bounded-recovery-{number}",
        )
        for number in range(2)
    ]

    expired = coordinator.recover(max_transactions=1, deadline=time.monotonic() - 1)
    recovered = coordinator.recover(max_transactions=1, deadline=time.monotonic() + 5)

    assert expired == []
    assert len(recovered) == 1
    assert recovered[0].id in {item.id for item in transactions}


def test_recovery_honors_cancellation_before_next_transaction(
    vault: Path, state_root: Path
):
    coordinator = MarkdownCoordinator(vault, state_root)
    for number in range(2):
        coordinator.prepare(
            [MarkdownChange.create(f"knowledge/notes/cancel-{number}.md", b"new")],
            operation_id=f"cancel-recovery-{number}",
        )
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks > 2

    recovered = coordinator.recover(
        max_transactions=2,
        deadline=time.monotonic() + 5,
        cancelled=cancelled,
    )

    assert len(recovered) <= 1


def test_recovery_rolls_back_state_when_deadline_expires_before_sql_commit(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/recover-fence.md", b"new")],
        operation_id="recover-precommit-fence",
    )
    expired = False

    def cancelled() -> bool:
        return expired

    @contextmanager
    def expire_before_commit(database, *, before_commit=None):
        nonlocal expired
        statements = []
        database.set_trace_callback(statements.append)
        database.execute("BEGIN IMMEDIATE")
        try:
            yield database
            expires_here = any(
                'UPDATE "transaction" SET state = \'applying\'' in statement
                for statement in statements
            )
            if expires_here:
                expired = True
            if before_commit is not None:
                before_commit()
            database.commit()
        except BaseException:
            database.rollback()
            raise

    monkeypatch.setattr(markdown_transaction, "begin_immediate", expire_before_commit)

    with pytest.raises(TimeoutError, match="deadline"):
        coordinator.recover(
            deadline=time.monotonic() + 5,
            cancelled=cancelled,
        )

    assert coordinator._record(transaction.id).state == "prepared"
    assert not (vault / "knowledge/notes/recover-fence.md").exists()


def test_two_subprocesses_recover_the_same_transaction_safely(
    vault: Path, state_root: Path, tmp_path: Path
):
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/new.md", b"new")],
        operation_id="process-recovery",
    )
    scripts = Path(__file__).parents[1] / "scripts"
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    code = """
import json
import sys
import time
from pathlib import Path
from markdown_transaction import MarkdownCoordinator
vault, state, barrier, name = map(Path, sys.argv[1:])
(barrier / name).write_text('ready', encoding='ascii')
deadline = time.monotonic() + 10
while len(list(barrier.glob('ready-*'))) < 2:
    if time.monotonic() >= deadline:
        raise TimeoutError('barrier')
    time.sleep(0.01)
records = MarkdownCoordinator(vault, state).recover()
print(json.dumps([record.state for record in records]))
"""
    env = os.environ | {"PYTHONPATH": str(scripts)}
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                code,
                str(vault),
                str(state_root),
                str(barrier),
                f"ready-{number}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for number in range(2)
    ]
    outputs = [process.communicate(timeout=20) for process in processes]

    assert [process.returncode for process in processes] == [0, 0], outputs
    states = [json.loads(stdout) for stdout, _ in outputs]
    assert sorted(states, key=len) == [[], ["committed"]]
    assert coordinator._record(transaction.id).state == "committed"
    assert (vault / "knowledge/notes/new.md").read_bytes() == b"new"


def test_external_after_apply_mismatch_is_not_overwritten(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="after-mismatch",
    )
    original = coordinator._mutate_and_mark

    def mutate_then_edit(*args, **kwargs):
        original(*args, **kwargs)
        target.write_bytes(b"external-after")

    monkeypatch.setattr(coordinator, "_mutate_and_mark", mutate_then_edit)
    with pytest.raises(RuntimeError, match="after state"):
        coordinator.apply(transaction.id)

    assert target.read_bytes() == b"external-after"
    assert _row(state_root, transaction.id)["state"] == "conflicted"


def test_undo_is_new_forward_transaction_with_parent(vault: Path, state_root: Path):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    original = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="original",
    )
    coordinator.apply(original.id)

    undo = coordinator.undo(original.id)

    assert undo.id != original.id
    assert undo.parent_transaction_id == original.id
    assert undo.state == "prepared"
    assert coordinator.apply(undo.id).state == "committed"
    assert target.read_bytes() == b"before"


def test_undo_expired_deadline_does_not_prepare_inverse(vault: Path, state_root: Path):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    original = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="expired-undo-original",
    )
    coordinator.apply(original.id)

    with pytest.raises(TimeoutError, match="deadline"):
        coordinator.undo(original.id, deadline=time.monotonic() - 1)

    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        count = database.execute('SELECT COUNT(*) FROM "transaction"').fetchone()[0]
    assert count == 1
    assert target.read_bytes() == b"after"


def _inserts_a_transaction(statements: list) -> bool:
    return any('INSERT INTO "transaction"' in statement for statement in statements)


def _run_if_given(hook) -> None:
    if hook is not None:
        hook()


def test_undo_rolls_back_prepare_when_cancelled_at_sql_commit(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    original = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="undo-precommit-original",
    )
    coordinator.apply(original.id)
    expired = False
    commits = 0

    def cancelled() -> bool:
        return expired

    @contextmanager
    def expire_before_commit(database, *, before_commit=None):
        nonlocal commits, expired
        statements = []
        database.set_trace_callback(statements.append)
        database.execute("BEGIN IMMEDIATE")
        try:
            yield database
            expires_here = _inserts_a_transaction(statements)
            expired = expired or expires_here
            _run_if_given(before_commit)
            database.commit()
            commits += int(expires_here)
        except BaseException:
            database.rollback()
            raise

    monkeypatch.setattr(markdown_transaction, "begin_immediate", expire_before_commit)

    with pytest.raises(TimeoutError, match="deadline"):
        coordinator.undo(
            original.id,
            deadline=time.monotonic() + 5,
            cancelled=cancelled,
        )

    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        count = database.execute('SELECT COUNT(*) FROM "transaction"').fetchone()[0]
    assert commits == 0
    assert count == 1
    assert target.read_bytes() == b"after"


def test_apply_expired_deadline_does_not_mutate_target(vault: Path, state_root: Path):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="expired-apply",
    )

    with pytest.raises(TimeoutError, match="deadline"):
        coordinator.apply(transaction.id, deadline=time.monotonic() - 1)

    assert target.read_bytes() == b"before"
    assert coordinator._record(transaction.id).state == "prepared"


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (MarkdownChange.create("knowledge/notes/new.md", b"new"), None),
        (MarkdownChange.delete("knowledge/notes/page.md"), b"before"),
    ],
)
def test_undo_inverts_create_and_delete(
    vault: Path, state_root: Path, change: MarkdownChange, expected: bytes | None
):
    target = vault / change.path
    if change.kind == "delete":
        target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    original = coordinator.prepare([change], operation_id=f"original:{change.kind}")
    coordinator.apply(original.id)
    coordinator.apply(coordinator.undo(original.id).id)
    assert (target.read_bytes() if target.exists() else None) == expected


def test_undo_rejects_changed_current_targets(vault: Path, state_root: Path):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    original = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="original-changed",
    )
    coordinator.apply(original.id)
    target.write_bytes(b"external")

    with pytest.raises(RuntimeError, match="undo precondition"):
        coordinator.undo(original.id)
    assert target.read_bytes() == b"external"


def test_undo_rejects_transaction_outside_thirty_day_window(
    vault: Path, state_root: Path
):
    coordinator = MarkdownCoordinator(vault, state_root)
    original = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/new.md", b"new")],
        operation_id="expired-undo",
    )
    coordinator.apply(original.id)
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat().replace(
        "+00:00", "Z"
    )
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        database.execute(
            'UPDATE "transaction" SET updated_at = ? WHERE id = ?',
            (old, original.id),
        )
        database.commit()

    with pytest.raises(RuntimeError, match="undo window"):
        coordinator.undo(original.id)


def test_prune_retains_artifacts_for_the_window_then_removes_them(
    vault: Path, state_root: Path
):
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/new.md", b"new")],
        operation_id="prune-old",
    )
    coordinator.apply(transaction.id)
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        database.execute(
            'UPDATE "transaction" SET updated_at = ? WHERE id = ?',
            (created.isoformat().replace("+00:00", "Z"), transaction.id),
        )
        database.commit()

    window = timedelta(days=UNDO_RETENTION_DAYS)
    assert coordinator.prune(now=created + window) == 0
    assert (state_root / "run/transactions" / transaction.id).is_dir()
    assert coordinator.prune(now=created + window + timedelta(seconds=1)) == 1
    assert not (state_root / "run/transactions" / transaction.id).exists()


def test_prune_rolls_back_marker_when_cancelled_at_sql_commit(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/prune-fence.md", b"new")],
        operation_id="prune-precommit-fence",
    )
    coordinator.apply(transaction.id)
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        database.execute(
            'UPDATE "transaction" SET updated_at = ? WHERE id = ?',
            (old.isoformat().replace("+00:00", "Z"), transaction.id),
        )
        database.commit()
    expired = False

    def cancelled() -> bool:
        return expired

    @contextmanager
    def expire_before_commit(database, *, before_commit=None):
        nonlocal expired
        statements = []
        database.set_trace_callback(statements.append)
        database.execute("BEGIN IMMEDIATE")
        try:
            yield database
            expires_here = any(
                "SET artifacts_pruned_at" in statement for statement in statements
            )
            if expires_here:
                expired = True
            if before_commit is not None:
                before_commit()
            database.commit()
        except BaseException:
            database.rollback()
            raise

    monkeypatch.setattr(markdown_transaction, "begin_immediate", expire_before_commit)

    with pytest.raises(TimeoutError, match="deadline"):
        coordinator.prune(
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            deadline=time.monotonic() + 5,
            cancelled=cancelled,
        )

    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        pruned_at = database.execute(
            'SELECT artifacts_pruned_at FROM "transaction" WHERE id = ?',
            (transaction.id,),
        ).fetchone()[0]
    assert pruned_at is None
    assert (state_root / "run/transactions" / transaction.id).is_dir()


@pytest.mark.parametrize("retention_days", [-1, 0])
def test_prune_rejects_retention_shorter_than_fixed_undo_window(
    vault: Path, state_root: Path, retention_days: int
):
    """The floor is the undo window itself, not the literal 30 it used to be.

    The month-long window was exchanged on 2026-09-02 for a daily snapshot and a
    copy off the machine, after measuring that nothing ever called `prune` and
    the trail had reached 4.9 GB — half of it exact duplicates of a journal that
    grows one line at a time. What the floor still refuses is a window shorter
    than the one the contract states.
    """
    from markdown_transaction import UNDO_RETENTION_DAYS

    coordinator = MarkdownCoordinator(vault, state_root)
    with pytest.raises(ValueError, match=f"at least {UNDO_RETENTION_DAYS}"):
        coordinator.prune(retention_days=retention_days)


def test_prune_honors_supplied_retention_longer_than_thirty_days(
    vault: Path, state_root: Path
):
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/new.md", b"new")],
        operation_id="prune-sixty",
    )
    coordinator.apply(transaction.id)
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        database.execute(
            'UPDATE "transaction" SET updated_at = ? WHERE id = ?',
            (created.isoformat().replace("+00:00", "Z"), transaction.id),
        )
        database.commit()

    assert coordinator.prune(
        retention_days=60, now=created + timedelta(days=31)
    ) == 0
    assert (state_root / "run/transactions" / transaction.id).is_dir()


@pytest.mark.parametrize("state", ["prepared", "applying", "conflicted", "quarantined"])
def test_prune_never_removes_protected_states(
    vault: Path, state_root: Path, state: str
):
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/new.md", b"new")],
        operation_id=f"protected:{state}",
    )
    _set_state(state_root, transaction.id, state)
    old = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        database.execute(
            'UPDATE "transaction" SET updated_at = ? WHERE id = ?',
            (old, transaction.id),
        )
        database.commit()

    assert coordinator.prune(now=datetime(2026, 1, 1, tzinfo=timezone.utc)) == 0
    assert (state_root / "run/transactions" / transaction.id).is_dir()


def test_deletion_blockers_report_only_redacted_identifiers_states_and_codes(
    vault: Path, state_root: Path
):
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/secret-name.md", b"secret-body")],
        operation_id="secret-operation-name",
    )

    blockers = coordinator.deletion_blockers()
    rendered = json.dumps(blockers)

    assert blockers == [
        {
            "transaction_id": transaction.id,
            "state": "prepared",
            "code": "nonterminal_transaction",
        }
    ]
    assert "secret-name" not in rendered
    assert "secret-body" not in rendered
    assert "secret-operation-name" not in rendered


def test_cli_recover_undo_and_prune_are_explicit_and_redacted(
    vault: Path, state_root: Path
):
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/secret.md", b"secret")],
        operation_id="cli-secret",
    )
    script = Path(__file__).parents[1] / "scripts/markdown_transaction.py"
    env = os.environ | {
        "LLM_WIKI_ROOT": str(vault),
        "LLM_WIKI_STATE_ROOT": str(state_root),
    }

    recovered = subprocess.run(
        [sys.executable, str(script), "recover"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    payload = json.loads(recovered.stdout)
    assert payload == [
        {"transaction_id": transaction.id, "state": "committed", "code": None}
    ]
    assert "secret" not in recovered.stdout

    undone = subprocess.run(
        [sys.executable, str(script), "undo", transaction.id],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    undo_payload = json.loads(undone.stdout)
    assert undo_payload["state"] == "committed"
    assert undo_payload["parent_transaction_id"] == transaction.id
    assert not (vault / "knowledge/notes/secret.md").exists()

    pruned = subprocess.run(
        [sys.executable, str(script), "prune", "--retention-days", "30"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert json.loads(pruned.stdout) == {"pruned": 0}


def test_cli_domain_failure_is_canonical_bounded_and_redacted(
    vault: Path, state_root: Path
):
    target = vault / "knowledge/notes/private-name.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/private-name.md", b"after")],
        operation_id="private-operation",
    )
    coordinator.apply(transaction.id)
    target.write_bytes(b"private secret image bytes")
    script = Path(__file__).parents[1] / "scripts/markdown_transaction.py"
    env = os.environ | {
        "LLM_WIKI_ROOT": str(vault),
        "LLM_WIKI_STATE_ROOT": str(state_root),
    }

    failed = subprocess.run(
        [sys.executable, str(script), "undo", transaction.id],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert failed.returncode != 0
    assert failed.stderr == ""
    assert len(failed.stdout) <= 256
    assert failed.stdout == (
        '{"code":"undo_precondition_failed","state":"committed",'
        f'"transaction_id":"{transaction.id}"}}\n'
    )
    assert "private" not in failed.stdout
    assert "Traceback" not in failed.stdout + failed.stderr


def test_cli_conflict_report_is_canonical_and_redacted(vault: Path, state_root: Path):
    target = vault / "knowledge/notes/private-conflict.md"
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/private-conflict.md", b"after")],
        operation_id="private-conflict-operation",
    )
    target.write_bytes(b"private conflict bytes")
    script = Path(__file__).parents[1] / "scripts/markdown_transaction.py"
    env = os.environ | {
        "LLM_WIKI_ROOT": str(vault),
        "LLM_WIKI_STATE_ROOT": str(state_root),
    }

    result = subprocess.run(
        [sys.executable, str(script), "recover"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode != 0
    assert result.stderr == ""
    assert result.stdout == (
        '[{"code":"before_hash_mismatch","state":"conflicted",'
        f'"transaction_id":"{transaction.id}"}}]\n'
    )
    assert "private" not in result.stdout


def test_cli_rejects_short_prune_window_as_redacted_json(
    vault: Path, state_root: Path
):
    MarkdownCoordinator(vault, state_root)
    script = Path(__file__).parents[1] / "scripts/markdown_transaction.py"
    env = os.environ | {
        "LLM_WIKI_ROOT": str(vault),
        "LLM_WIKI_STATE_ROOT": str(state_root),
    }
    failed = subprocess.run(
        [
            sys.executable,
            str(script),
            "prune",
            "--retention-days",
            str(UNDO_RETENTION_DAYS - 1),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert failed.returncode != 0
    assert failed.stderr == ""
    assert failed.stdout == (
        '{"code":"retention_too_short","state":null,"transaction_id":null}\n'
    )


def test_record_exposes_absent_hash_without_target_content(vault: Path, state_root: Path):
    coordinator = MarkdownCoordinator(vault, state_root)
    record = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/new.md", b"content")],
        operation_id="record-shape",
    )
    assert record.operations[0].before_hash == ABSENT
