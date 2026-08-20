from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import markdown_transaction  # noqa: E402
import memory_queue  # noqa: E402
import operational_ownership  # noqa: E402
from markdown_transaction import MarkdownChange  # noqa: E402
from reliable_memory import sha256_bytes, validate_schema  # noqa: E402


def _abort_fixture(tmp_path: Path):
    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    (notes / "replace.md").write_bytes(b"replace-before")
    (notes / "delete.md").write_bytes(b"delete-before")
    candidate = tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(candidate, source_v2=None)
    coordinator = markdown_transaction.MarkdownCoordinator._from_v3_candidate(
        candidate, state_root=tmp_path
    )
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    intent_id = "f" * 64
    owner = registry.acquire(
        "repair", scope="repair:transaction-abort", actor_id="abort-repair"
    )
    fence = coordinator.acquire_intent_fence(intent_id, mode="operator", owner=owner)
    binding = memory_queue.CaptureTaskBinding(
        task_id="abort-task",
        intent_id=intent_id,
        intent_sha256="1" * 64,
        handler_version=1,
        active_digest="2" * 64,
        seal_digest="3" * 64,
    )
    coordinator.project_capture_binding(binding, intent_fence=fence)
    record = coordinator.prepare(
        [
            MarkdownChange.create("knowledge/notes/create.md", b"create-after"),
            MarkdownChange.replace("knowledge/notes/replace.md", b"replace-after"),
            MarkdownChange.delete("knowledge/notes/delete.md"),
        ],
        operation_id="abort-mixed-fixture",
    )
    plan = coordinator._load_verified_plan(record)
    rows = coordinator._operation_rows(record.id)
    with coordinator.writer_gate():
        for row, operation_plan in zip(rows, plan["operations"], strict=True):
            coordinator._mutate_and_mark(record.id, row, operation_plan)
    with coordinator._connect() as database:
        database.execute(
            'UPDATE "transaction" SET state="applying" WHERE id=?', (record.id,)
        )
    return coordinator, registry, owner, fence, binding, record


def test_abort_restores_mixed_before_images_and_publishes_exact_receipt(
    tmp_path: Path,
) -> None:
    coordinator, registry, owner, fence, binding, record = _abort_fixture(tmp_path)

    receipt = coordinator.abort_for_discard(
        record.id,
        intent_fence=fence,
        active_link_digest=binding.active_digest,
        actor_identity="posix-uid:1000",
    )

    assert not (tmp_path / "knowledge" / "notes" / "create.md").exists()
    assert (tmp_path / "knowledge" / "notes" / "replace.md").read_bytes() == b"replace-before"
    assert (tmp_path / "knowledge" / "notes" / "delete.md").read_bytes() == b"delete-before"
    receipt_path = tmp_path / receipt.receipt_path
    payload = json.loads(receipt_path.read_bytes())
    validate_schema(
        payload,
        SCRIPTS_DIR / "schemas" / "transaction-abort-v1.json",
    )
    assert receipt_path.stat().st_size <= 64 * 1024
    assert sha256_bytes(receipt_path.read_bytes()) == receipt.receipt_sha256
    assert payload["intent_fence_token_sha256"] == sha256_bytes(
        fence.token.encode("utf-8")
    )
    assert payload["restored_target_count"] == 3
    with sqlite3.connect(coordinator.database_path) as database:
        row = database.execute(
            """SELECT state,abort_operation_id,abort_manifest_sha256,
                      abort_receipt_sha256,aborted_at
               FROM "transaction" WHERE id=?""",
            (record.id,),
        ).fetchone()
    assert row == (
        "aborted",
        receipt.abort_operation_id,
        payload["before_manifest_sha256"],
        receipt.receipt_sha256,
        receipt.aborted_at,
    )
    coordinator.release_intent_fence(fence)
    registry.release(owner)


def test_abort_target_conflict_stays_aborting_and_never_rolls_forward(
    tmp_path: Path,
) -> None:
    coordinator, registry, owner, fence, binding, record = _abort_fixture(tmp_path)
    target = tmp_path / "knowledge" / "notes" / "replace.md"
    target.write_bytes(b"third-party")

    with pytest.raises(markdown_transaction.TransactionFailure) as error:
        coordinator.abort_for_discard(
            record.id,
            intent_fence=fence,
            active_link_digest=binding.active_digest,
            actor_identity="posix-uid:1000",
        )

    assert error.value.code == "abort_target_conflict"
    assert target.read_bytes() == b"third-party"
    with sqlite3.connect(coordinator.database_path) as database:
        assert database.execute(
            'SELECT state,error_code FROM "transaction" WHERE id=?', (record.id,)
        ).fetchone() == ("aborting", "abort_target_conflict")
    assert not (
        tmp_path / "run" / "transactions" / record.id / "abort-receipt.json"
    ).exists()
    coordinator.release_intent_fence(fence)
    registry.release(owner)


def test_recovery_of_aborting_transaction_is_rollback_only(tmp_path: Path) -> None:
    coordinator, registry, owner, fence, binding, record = _abort_fixture(tmp_path)

    def stop_after_direction(name: str, _parent: str | None = None) -> None:
        if name == "after_aborting":
            raise RuntimeError("simulated crash after abort direction")

    coordinator._killpoint = stop_after_direction  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="simulated crash"):
        coordinator.abort_for_discard(
            record.id,
            intent_fence=fence,
            active_link_digest=binding.active_digest,
            actor_identity="posix-uid:1000",
        )

    recovered = markdown_transaction.MarkdownCoordinator._from_v3_candidate(
        coordinator.database_path, state_root=tmp_path
    ).recover()

    assert recovered[0].state == "aborting"
    assert recovered[0].error_code == "abort_receipt_pending"
    assert not (tmp_path / "knowledge" / "notes" / "create.md").exists()
    assert (tmp_path / "knowledge" / "notes" / "replace.md").read_bytes() == b"replace-before"
    assert (tmp_path / "knowledge" / "notes" / "delete.md").read_bytes() == b"delete-before"
    coordinator.release_intent_fence(fence)
    registry.release(owner)


def test_recovery_flags_aborted_transaction_with_missing_receipt(tmp_path: Path) -> None:
    coordinator, registry, owner, fence, binding, record = _abort_fixture(tmp_path)
    receipt = coordinator.abort_for_discard(
        record.id,
        intent_fence=fence,
        active_link_digest=binding.active_digest,
        actor_identity="posix-uid:1000",
    )
    (tmp_path / receipt.receipt_path).unlink()

    recovered = markdown_transaction.MarkdownCoordinator._from_v3_candidate(
        coordinator.database_path, state_root=tmp_path
    ).recover()

    assert recovered[0].state == "aborted"
    assert recovered[0].error_code == "abort_receipt_invalid"
    coordinator.release_intent_fence(fence)
    registry.release(owner)


def test_abort_refuses_to_restore_a_corrupt_before_image(tmp_path: Path) -> None:
    """A before-image that no longer hashes must never be written back."""
    coordinator, registry, owner, fence, binding, record = _abort_fixture(tmp_path)
    rows = coordinator._operation_rows(record.id)
    replace_row = next(row for row in rows if str(row["path"]).endswith("replace.md"))
    artifact = (
        tmp_path
        / "run"
        / "transactions"
        / record.id
        / "before"
        / f"{int(replace_row['position']):06d}.bin"
    )
    artifact.write_bytes(b"corrupted-before-image")

    with pytest.raises(markdown_transaction.TransactionFailure) as error:
        coordinator.abort_for_discard(
            record.id,
            intent_fence=fence,
            active_link_digest=binding.active_digest,
            actor_identity="posix-uid:1000",
        )

    assert error.value.code == "abort_before_image_corrupt"
    target = tmp_path / "knowledge" / "notes" / "replace.md"
    assert target.read_bytes() == b"replace-after"
    with sqlite3.connect(coordinator.database_path) as database:
        assert database.execute(
            'SELECT state,error_code FROM "transaction" WHERE id=?', (record.id,)
        ).fetchone() == ("aborting", "abort_before_image_corrupt")
    assert not (
        tmp_path / "run" / "transactions" / record.id / "abort-receipt.json"
    ).exists()
    coordinator.release_intent_fence(fence)
    registry.release(owner)


def test_abort_refuses_a_released_intent_fence(tmp_path: Path) -> None:
    """Only a live operator fence may discard applied work."""
    coordinator, registry, owner, fence, binding, record = _abort_fixture(tmp_path)
    coordinator.release_intent_fence(fence)

    with pytest.raises(markdown_transaction.TransactionFailure) as error:
        coordinator.abort_for_discard(
            record.id,
            intent_fence=fence,
            active_link_digest=binding.active_digest,
            actor_identity="posix-uid:1000",
        )

    assert error.value.code == "intent_fence_lost"
    assert (
        tmp_path / "knowledge" / "notes" / "replace.md"
    ).read_bytes() == b"replace-after"
    with sqlite3.connect(coordinator.database_path) as database:
        assert database.execute(
            'SELECT state FROM "transaction" WHERE id=?', (record.id,)
        ).fetchone() == ("applying",)
    registry.release(owner)


def test_abort_refuses_a_committed_transaction(tmp_path: Path) -> None:
    """Committed bytes are the vault's history; abort must not touch them."""
    coordinator, registry, owner, fence, binding, record = _abort_fixture(tmp_path)
    with coordinator._connect() as database:
        database.execute(
            'UPDATE "transaction" SET state=\'committed\' WHERE id=?', (record.id,)
        )

    with pytest.raises(markdown_transaction.TransactionFailure) as error:
        coordinator.abort_for_discard(
            record.id,
            intent_fence=fence,
            active_link_digest=binding.active_digest,
            actor_identity="posix-uid:1000",
        )

    assert error.value.code == "abort_committed"
    assert (
        tmp_path / "knowledge" / "notes" / "replace.md"
    ).read_bytes() == b"replace-after"
    coordinator.release_intent_fence(fence)
    registry.release(owner)
