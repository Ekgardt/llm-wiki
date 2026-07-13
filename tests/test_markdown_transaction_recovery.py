from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from markdown_transaction import ABSENT, MarkdownChange, MarkdownCoordinator
from reliable_memory import sha256_bytes


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


def test_corrupt_after_image_rolls_back_only_known_transaction_hashes(
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

    assert recovered.state == "discarded"
    assert recovered.error_code == "after_image_corrupt"
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


@pytest.mark.parametrize("expire_after_call", [1, 2])
def test_obsolete_lease_after_partial_checkpoint_rolls_back_journal_and_projection(
    vault: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    expire_after_call: int,
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

    def expire_after_journal(*args, **kwargs):
        nonlocal calls
        mutate(*args, **kwargs)
        calls += 1
        if calls == expire_after_call:
            with sqlite3.connect(
                state_root / "run/markdown-transactions.sqlite3"
            ) as database:
                database.execute(
                    "UPDATE project_leases SET lease_token = 'new-token', fencing_epoch = 2 "
                    "WHERE project = 'demo'"
                )
                database.commit()

    monkeypatch.setattr(coordinator, "_mutate_and_mark", expire_after_journal)

    with pytest.raises(RuntimeError, match="precondition"):
        coordinator.apply(transaction.id)

    record = coordinator._record(transaction.id)
    assert (record.state, record.error_code) == ("quarantined", "precondition_failed")
    assert journal.read_bytes() == b"old-event\n"
    assert projection.read_bytes() == b"index-v1\n"


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
            with sqlite3.connect(
                state_root / "run/markdown-transactions.sqlite3"
            ) as database:
                database.execute(
                    "UPDATE project_leases SET lease_token = 'new-token', fencing_epoch = 2 "
                    "WHERE project = 'demo'"
                )
                database.commit()

    monkeypatch.setattr(coordinator, "_mutate_and_mark", external_edit_after_journal)

    with pytest.raises(RuntimeError, match="precondition"):
        coordinator.apply(transaction.id)

    assert coordinator._record(transaction.id).state == "quarantined"
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


def test_prune_retains_artifacts_for_thirty_days_then_removes_them(
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

    assert coordinator.prune(now=created + timedelta(days=30)) == 0
    assert (state_root / "run/transactions" / transaction.id).is_dir()
    assert coordinator.prune(now=created + timedelta(days=30, seconds=1)) == 1
    assert not (state_root / "run/transactions" / transaction.id).exists()


@pytest.mark.parametrize("retention_days", [-1, 0, 29])
def test_prune_rejects_retention_shorter_than_fixed_undo_window(
    vault: Path, state_root: Path, retention_days: int
):
    coordinator = MarkdownCoordinator(vault, state_root)
    with pytest.raises(ValueError, match="at least 30"):
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
        [sys.executable, str(script), "prune", "--retention-days", "29"],
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
