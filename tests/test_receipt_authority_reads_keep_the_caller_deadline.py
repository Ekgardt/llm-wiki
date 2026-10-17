from __future__ import annotations

import sqlite3
import time

import compile_memory as compiler
import pytest
from markdown_transaction import MarkdownChange, MarkdownCoordinator

from tests.test_a_continuation_keeps_its_original_entry import _published_continuation
from tests.test_compile_transactions import vault as vault


def test_an_expired_authority_read_cannot_report_missing(vault):
    root, state_root = vault
    coordinator = MarkdownCoordinator(root, state_root)
    with pytest.raises(TimeoutError, match="authority read deadline"):
        coordinator.committed_attempt("absent", deadline=time.monotonic() - 1)


def test_a_locked_database_cannot_spend_the_default_wait(vault):
    root, state_root = vault
    coordinator = MarkdownCoordinator(root, state_root)
    lock = sqlite3.connect(coordinator.database_path)
    lock.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    try:
        with pytest.raises((TimeoutError, sqlite3.OperationalError)):
            coordinator.committed_attempt("absent", deadline=started + 0.05)
    finally:
        lock.rollback()
        lock.close()
    assert time.monotonic() - started < 0.5


def test_a_running_authority_statement_is_interrupted(vault):
    root, state_root = vault
    coordinator = MarkdownCoordinator(root, state_root)
    with pytest.raises(TimeoutError, match="authority read deadline"):
        with coordinator._authority_read_connection(time.monotonic() + 0.02) as database:
            database.execute(
                "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n"
            ).fetchone()


def test_existing_no_deadline_readers_remain_compatible(vault):
    root, state_root = vault
    coordinator = MarkdownCoordinator(root, state_root)
    assert coordinator.committed_attempt("absent") is None
    assert coordinator.committed_attempt("absent", deadline=float("inf")) is None


def test_the_record_lookup_cannot_bypass_an_expired_deadline(vault):
    _path, coordinator, part = _published_continuation(vault)
    receipt = compiler._read_snapshot_receipt(part, coordinator)
    transaction = coordinator.committed_attempt(receipt["operation_id"])
    assert transaction is not None
    with pytest.raises(TimeoutError, match="authority read deadline"):
        coordinator._record(transaction.id, deadline=time.monotonic() - 1)


def test_an_ordinal_authority_read_preserves_the_same_deadline(vault):
    root, state_root = vault
    coordinator = MarkdownCoordinator(root, state_root)
    transaction = coordinator.prepare(
        (MarkdownChange.create("knowledge/notes/ordinal-proof.md", b"---\ntype: concept\n---\n# Ordinal proof\n"),),
        operation_id="authority-proof#1",
    )
    committed = coordinator.apply(transaction.id)
    assert coordinator.committed_attempt("authority-proof", deadline=time.monotonic() + 1).id == committed.id
    with pytest.raises(TimeoutError, match="authority read deadline"):
        coordinator._committed_attempt_by_ordinal("authority-proof", deadline=time.monotonic() - 1)
