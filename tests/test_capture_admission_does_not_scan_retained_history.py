"""Normal admission is bounded by its contract, not every retained ledger row."""

import shutil
import sqlite3

import installed_memory_repair as repair
import markdown_transaction as transactions
import memory_queue
import pytest
import reliable_memory

from tests.test_doctor import _adopt, _build_root


def _adopted(tmp_path, monkeypatch):
    root, state, _home = _build_root(tmp_path)
    _adopt(root, state)
    monkeypatch.setattr(transactions, "_ADOPTION_VALIDATION_CACHE", set())
    return root, state


def _history_authorizer(action, argument, _column, _database, _trigger):
    forbidden = {
        sqlite3.SQLITE_PRAGMA: {"integrity_check", "foreign_key_check"},
        sqlite3.SQLITE_READ: {"operation", "transaction"},
    }
    if argument in forbidden.get(action, set()):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _restrict_history_reads(monkeypatch):
    real = reliable_memory.open_readonly_operational_db

    def opened(*args, **kwargs):
        database = real(*args, **kwargs)
        database.set_authorizer(_history_authorizer)
        return database

    for module in (repair, transactions, memory_queue):
        monkeypatch.setattr(module, "open_readonly_operational_db", opened)


def test_normal_admission_does_not_read_unrelated_retained_history(tmp_path, monkeypatch):
    root, state = _adopted(tmp_path, monkeypatch)
    with pytest.MonkeyPatch.context() as restricted:
        _restrict_history_reads(restricted)
        queue = memory_queue.active_memory_queue(root, state)
        coordinator = transactions.active_markdown_coordinator(root, state)
        paired = queue._intent_coordinator()
        assert paired.database_path == coordinator.database_path
    assert queue.db_path == state / "run/queue-v3.sqlite3"
    record = coordinator.prepare(
        [transactions.MarkdownChange.create("knowledge/notes/new.md", b"new durable evidence")],
        operation_id="history-independent-publication",
    )
    committed = coordinator.apply(record.id)
    assert committed.state == "committed"
    assert (root / "knowledge/notes/new.md").read_bytes() == b"new durable evidence"


def test_full_adoption_certification_still_reads_the_complete_database(tmp_path, monkeypatch):
    root, state = _adopted(tmp_path, monkeypatch)
    _restrict_history_reads(monkeypatch)
    with pytest.raises(repair.ReliabilityV3ValidationError, match="not authorized"):
        repair.require_reliability_v3_adopted(root=root, state_root=state)


@pytest.mark.parametrize("pragma", ["application_id", "user_version"])
def test_normal_coordinator_admission_refuses_changed_database_metadata(
    tmp_path, monkeypatch, pragma
):
    root, state = _adopted(tmp_path, monkeypatch)
    transactions.active_markdown_coordinator(root, state)
    with sqlite3.connect(state / "run/markdown-transactions-v3.sqlite3") as database:
        database.execute(f"PRAGMA {pragma}=0")
    with pytest.raises((ValueError, sqlite3.DatabaseError, repair.ReliabilityV3ValidationError)):
        transactions.active_markdown_coordinator(root, state)


@pytest.mark.parametrize("name", ["queue-v3.sqlite3", "markdown-transactions-v3.sqlite3"])
def test_cached_admission_refuses_a_replaced_member_of_the_database_pair(
    tmp_path, monkeypatch, name
):
    root, state = _adopted(tmp_path, monkeypatch)
    memory_queue.active_memory_queue(root, state)
    path = state / "run" / name
    replacement = path.with_suffix(".replacement")
    shutil.copy2(path, replacement)
    replacement.replace(path)
    with pytest.raises(repair.ReliabilityV3ValidationError, match="artifact identity changed"):
        memory_queue.active_memory_queue(root, state)


def test_normal_coordinator_admission_refuses_an_incomplete_schema(tmp_path, monkeypatch):
    root, state = _adopted(tmp_path, monkeypatch)
    transactions.active_markdown_coordinator(root, state)
    with sqlite3.connect(state / "run/markdown-transactions-v3.sqlite3") as database:
        database.execute('DROP TABLE "operation"')
    with pytest.raises(reliable_memory.OperationalDatabaseContractError, match="schema"):
        transactions.active_markdown_coordinator(root, state)


def test_complete_coordinator_certification_still_refuses_corrupt_retained_operations(tmp_path, monkeypatch):
    root, state = _adopted(tmp_path, monkeypatch)
    coordinator = transactions.active_markdown_coordinator(root, state)
    record = coordinator.prepare([transactions.MarkdownChange.create("knowledge/notes/retained.md", b"retained source")], operation_id="retained-history")
    assert coordinator.apply(record.id).state == "committed"
    with sqlite3.connect(coordinator.database_path) as database:
        database.execute('UPDATE "operation" SET position=-1 WHERE transaction_id=?', (record.id,))
    with pytest.raises(reliable_memory.OperationalDatabaseContractError, match="invariant"):
        transactions.validate_coordinator_v3_database(coordinator.database_path, state_root=state)
    with pytest.raises(repair.ReliabilityV3ValidationError, match="invariant"):
        repair.require_reliability_v3_adopted(root=root, state_root=state)
