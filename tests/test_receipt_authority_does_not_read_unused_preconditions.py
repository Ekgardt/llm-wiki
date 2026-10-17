from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager

import compile_memory as compiler
import pytest
from markdown_transaction import MarkdownChange, MarkdownCoordinator

from tests.test_a_continuation_keeps_its_original_entry import _published_continuation
from tests.test_compile_transactions import vault as vault


def _authority_columns_only(action, table, column, *_context):
    if (action, table, column) == (sqlite3.SQLITE_READ, "transaction", "preconditions_json"):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _observe_authority_connection(monkeypatch, coordinator):
    original = coordinator._connect

    @contextmanager
    def connected(*args, **kwargs):
        with original(*args, **kwargs) as database:
            database.set_authorizer(_authority_columns_only)
            yield database

    monkeypatch.setattr(coordinator, "_connect", connected)


def test_receipt_authority_never_fetches_the_unused_preconditions(vault, monkeypatch):
    _path, coordinator, part = _published_continuation(vault)
    original = compiler._read_snapshot_receipt(part, coordinator)
    _observe_authority_connection(monkeypatch, coordinator)
    assert compiler._read_snapshot_receipt(part, coordinator) == original


def test_projected_write_matches_every_recorded_operation(vault):
    _path, coordinator, part = _published_continuation(vault)
    receipt = compiler._read_snapshot_receipt(part, coordinator)
    complete = coordinator.committed_attempt(receipt["operation_id"])
    authority = coordinator.committed_write_authority(receipt["operation_id"])
    assert (authority.id, authority.operation_id, authority.state, authority.operations) == (
        complete.id, complete.operation_id, complete.state, complete.operations
    )
    assert not hasattr(authority, "preconditions")
    assert complete.preconditions


def _committed_ordinal(coordinator, identity, ordinal):
    prepared = coordinator.prepare(
        (MarkdownChange.create(f"knowledge/notes/ordinal-{ordinal}.md", b"# Evidence\n"),),
        operation_id=f"{identity}#{ordinal}",
    )
    return coordinator.apply(prepared.id)


@pytest.mark.parametrize("identity", ("proof", "proof_%\\", "доказательство"))
def test_projected_authority_retains_latest_literal_ordinal(vault, identity):
    root, state = vault
    coordinator = MarkdownCoordinator(root, state)
    _committed_ordinal(coordinator, identity, 1)
    latest = _committed_ordinal(coordinator, identity, 2)
    projected = coordinator.committed_write_authority(identity)
    assert projected.id == latest.id and projected.operations == latest.operations
    assert coordinator.committed_write_authority(identity + "-other") is None


def test_projection_does_not_claim_prepared_work_is_committed(vault):
    root, state = vault
    coordinator = MarkdownCoordinator(root, state)
    prepared = coordinator.prepare(
        (MarkdownChange.create("knowledge/notes/not-written.md", b"# Pending\n"),),
        operation_id="not-written",
    )
    assert coordinator.committed_write_authority("not-written") is None
    assert coordinator._record(prepared.id).state == "prepared"


def test_projected_expired_read_never_reports_missing(vault):
    root, state = vault
    coordinator = MarkdownCoordinator(root, state)
    with pytest.raises(TimeoutError, match="authority read deadline"):
        coordinator.committed_write_authority("absent", deadline=time.monotonic() - 1)


def test_projected_locked_read_keeps_the_real_caller_clock(vault):
    root, state = vault
    coordinator = MarkdownCoordinator(root, state)
    lock = sqlite3.connect(coordinator.database_path)
    lock.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    try:
        with pytest.raises((TimeoutError, sqlite3.OperationalError)):
            coordinator.committed_write_authority("absent", deadline=started + 0.05)
    finally:
        lock.rollback()
        lock.close()
    assert time.monotonic() - started < 0.5
