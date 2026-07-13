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


def test_record_exposes_absent_hash_without_target_content(vault: Path, state_root: Path):
    coordinator = MarkdownCoordinator(vault, state_root)
    record = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/new.md", b"content")],
        operation_id="record-shape",
    )
    assert record.operations[0].before_hash == ABSENT
