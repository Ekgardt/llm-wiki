from __future__ import annotations

import json
from datetime import datetime, timezone

import doctor
import markdown_transaction
import pytest
from review_refused_compile import reject_draft, reviewed_refusal

from tests.test_reliability_v3_adoption import _vault, build_adopted_reliability_v3


def _blocked(content):
    raise markdown_transaction.DLPContentBlocked("protected test content")


@pytest.fixture
def refused(tmp_path, monkeypatch):
    root, state = _vault(tmp_path)
    build_adopted_reliability_v3(root, state)
    coordinator = markdown_transaction.active_markdown_coordinator(root, state)
    target = root / "knowledge/notes/rejected.md"
    record = coordinator.prepare(
        [markdown_transaction.MarkdownChange.create("knowledge/notes/rejected.md", b"Unsupported claim\n")],
        operation_id="compile:review-fixture", content_guard="model_output",
    )
    with monkeypatch.context() as patch:
        patch.setattr(markdown_transaction, "require_safe_publication", _blocked)
        with pytest.raises(markdown_transaction.TransactionFailure):
            coordinator.apply(record.id)
    assert coordinator.transaction_state(record.id) == "quarantined"
    return coordinator, record.id, target


def _status(coordinator):
    return doctor._transaction_check(
        coordinator.state_root, datetime.now(timezone.utc), vault_root=coordinator.vault,
    )


def _reject(coordinator, identifier):
    return reject_draft(coordinator, identifier, actor="test operator", reason="Unsupported outcome; source retained.")


def test_review_is_visible_without_publishing_rejected_content(refused):
    coordinator, identifier, target = refused
    assert _status(coordinator)["details"]["quarantined_unresolved"] == 1
    _reject(coordinator, identifier)
    report = _status(coordinator)
    assert report["details"]["quarantined_unresolved"] == 0
    assert report["details"]["quarantined_reviewed"] == 1
    assert not target.exists()
    assert coordinator.transaction_state(identifier) == "quarantined"
    assert "transaction_quarantined" in report["details"]["deletion_codes"]


def test_review_replay_is_idempotent_and_different_review_is_refused(refused):
    coordinator, identifier, _ = refused
    first = _reject(coordinator, identifier)
    assert _reject(coordinator, identifier) == first
    with pytest.raises(ValueError, match="different decision"):
        reject_draft(coordinator, identifier, actor="other", reason="different")


@pytest.mark.parametrize("field,value", [("transaction_id", "0" * 32), ("reason", ""), ("binding", {})])
def test_changed_review_cannot_clear_the_refusal(refused, field, value):
    coordinator, identifier, _ = refused
    _reject(coordinator, identifier)
    path = coordinator.transaction_root / identifier / "operator-review.json"
    review = json.loads(path.read_text())
    review[field] = value
    path.write_text(json.dumps(review))
    assert _status(coordinator)["details"]["quarantined_unresolved"] == 1


def test_review_of_applied_work_is_refused(refused):
    coordinator, identifier, _ = refused
    with coordinator._connect() as database:
        database.execute('UPDATE operation SET applied=1 WHERE transaction_id=?', (identifier,))
    with pytest.raises(ValueError, match="applied"):
        _reject(coordinator, identifier)


def test_changed_plan_invalidates_review(refused):
    coordinator, identifier, _ = refused
    _reject(coordinator, identifier)
    plan = coordinator.transaction_root / identifier / "plan.json"
    plan.write_bytes(plan.read_bytes() + b" ")
    with coordinator._connect() as database:
        assert not reviewed_refusal(database, identifier, coordinator.state_root)


@pytest.mark.parametrize("column,value", [("operation_id", "capture:unreviewable"), ("state", "committed")])
def test_other_work_is_not_operator_rejectable(refused, column, value):
    coordinator, identifier, _ = refused
    with coordinator._connect() as database:
        database.execute(f'UPDATE "transaction" SET {column}=? WHERE id=?', (value, identifier))
    with pytest.raises(ValueError, match="quarantined compile"):
        _reject(coordinator, identifier)


def test_previously_reviewed_work_becoming_applied_is_not_cleared(refused):
    coordinator, identifier, _ = refused
    _reject(coordinator, identifier)
    with coordinator._connect() as database:
        database.execute('UPDATE operation SET applied=1 WHERE transaction_id=?', (identifier,))
        assert not reviewed_refusal(database, identifier, coordinator.state_root)


def test_repair_does_not_resurrect_an_explicitly_rejected_draft(refused):
    import repair_refused_page_creation as repair

    coordinator, identifier, _ = refused
    with coordinator._connect() as database:
        database.execute('UPDATE "transaction" SET error_code=? WHERE id=?', ("precondition_failed", identifier))
    assert len(repair._collect(coordinator, coordinator.vault)) == 1
    _reject(coordinator, identifier)
    assert repair._collect(coordinator, coordinator.vault) == []


def test_invalid_review_cannot_be_ignored_by_repair(refused):
    import repair_refused_page_creation as repair

    coordinator, identifier, _ = refused
    with coordinator._connect() as database:
        database.execute('UPDATE "transaction" SET error_code=? WHERE id=?', ("precondition_failed", identifier))
    _reject(coordinator, identifier)
    (coordinator.transaction_root / identifier / "operator-review.json").write_text("invalid")
    with pytest.raises(ValueError, match="inspection"):
        repair._collect(coordinator, coordinator.vault)
