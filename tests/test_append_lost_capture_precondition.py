"""A lost capture claim must stop CAS retry without losing its evidence."""
import time

import markdown_transaction
import pytest
from reliable_memory import sha256_bytes

from tests import test_breadcrumb_worker
from tests.slow_machine import LONG_TIMEOUT


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    return test_breadcrumb_worker.delivery.__wrapped__(tmp_path, monkeypatch)


def _lose_claim(coordinator, conditions, fault):
    statements = {
        "expiry": "UPDATE intent_fences SET expires_at='2000-01-01T00:00:00Z' WHERE intent_id=?",
        "binding": "DELETE FROM capture_binding_projections WHERE intent_id=?",
    }
    with coordinator._connect() as database:
        database.execute(
            statements[fault], (conditions["intent_fence"]["intent_id"],),
        )


def _interrupt_after_real_refusals(monkeypatch, coordinator, attempts, fault):
    prepare = coordinator.prepare
    append = markdown_transaction.append_captured_knowledge

    def lose_claim(*args, **kwargs):
        conditions = kwargs.get("preconditions", {})
        if kwargs["operation_id"].startswith("breadcrumb:"):
            attempts.append(kwargs["operation_id"])
            _lose_claim(coordinator, conditions, fault)
        return prepare(*args, **kwargs)

    def cancelled():
        return len(attempts) >= 2

    def cancellable_append(*args, **kwargs):
        kwargs["cancelled"] = cancelled
        return append(*args, **kwargs)

    monkeypatch.setattr(coordinator, "prepare", lose_claim)
    monkeypatch.setattr(markdown_transaction, "append_captured_knowledge", cancellable_append)


@pytest.mark.parametrize("fault", ["expiry", "binding"])
def test_lost_claim_stops_after_the_first_real_refusal(delivery, monkeypatch, fault):
    queue, coordinator, publication, work = delivery
    attempts = []
    _interrupt_after_real_refusals(monkeypatch, coordinator, attempts, fault)

    with pytest.raises(markdown_transaction.TransactionFailure) as failure:
        work()

    assert failure.value.code == "precondition_failed"
    assert len(attempts) == 1
    assert not (queue.vault / "knowledge/daily/2026-09-29.md").exists()
    assert list((queue.state_root / "run/capture-intents").rglob("*.part"))
    assert not (queue.state_root / f"run/queue-results/capture-{publication.intent_id}.json").exists()


def _source_append(tmp_path):
    root = tmp_path / "vault"
    (root / "knowledge/daily").mkdir(parents=True)
    source = root / "knowledge/daily/source.md"
    source.write_bytes(b"original source\n")
    coordinator = markdown_transaction.MarkdownCoordinator(root, tmp_path / "state")
    conditions = {"knowledge/daily/source.md": sha256_bytes(source.read_bytes())}
    return coordinator, source, conditions


def _change_after_prepare(monkeypatch, coordinator, changed_path, attempts):
    prepare = coordinator.prepare

    def interrupted(*args, **kwargs):
        record = prepare(*args, **kwargs)
        attempts.append(record.id)
        if len(attempts) == 1:
            changed_path.write_bytes(b"external edit\n")
        return record

    monkeypatch.setattr(coordinator, "prepare", interrupted)


def _append_with_source(coordinator, conditions, cancelled):
    return markdown_transaction._append_until_committed(
        coordinator, "source-append", "knowledge/daily/target.md", b"accepted block\n",
        extra_preconditions=conditions, deadline=time.monotonic() + LONG_TIMEOUT,
        cancelled=cancelled,
    )


def test_changed_immutable_source_stops_retry(tmp_path, monkeypatch):
    coordinator, source, conditions = _source_append(tmp_path)
    attempts = []
    _change_after_prepare(monkeypatch, coordinator, source, attempts)

    def cancelled():
        return len(attempts) >= 2

    with pytest.raises(markdown_transaction.TransactionFailure):
        _append_with_source(coordinator, conditions, cancelled)

    assert len(attempts) == 1
    assert not (coordinator.vault / "knowledge/daily/target.md").exists()
    assert source.read_bytes() == b"external edit\n"


def test_target_cas_retries_while_immutable_source_is_valid(tmp_path, monkeypatch):
    coordinator, source, conditions = _source_append(tmp_path)
    target = coordinator.vault / "knowledge/daily/target.md"
    target.write_bytes(b"initial target\n")
    attempts = []
    _change_after_prepare(monkeypatch, coordinator, target, attempts)

    record = _append_with_source(coordinator, conditions, None)

    assert record.state == "committed"
    assert len(attempts) == 2
    assert target.read_bytes() == b"external edit\naccepted block\n"
    assert source.read_bytes() == b"original source\n"
