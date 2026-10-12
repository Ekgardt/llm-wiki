"""Archive receipt authority must not replace an expired clock with invalid evidence."""
from __future__ import annotations

import json
import sqlite3
import time
from unittest.mock import Mock

import archive_daily
import markdown_transaction
import pytest
import reliable_memory
from archive_daily import DailyArchiver

from tests.test_archive_daily_bagit import archive_vault as archive_vault


def _receipt(root):
    path = next((root / 'knowledge/daily/receipts').glob('v4-*.md'))
    record = json.loads(path.read_bytes().split(b'```json\n', 1)[1].split(b'\n```', 1)[0])
    return path, record


def test_expired_archive_constructor_creates_no_runtime(tmp_path):
    vault = tmp_path / 'vault'
    vault.mkdir()
    state = tmp_path / 'state'
    with pytest.raises(TimeoutError):
        DailyArchiver(vault, state, deadline=time.monotonic() - 1)
    assert not state.exists()


@pytest.mark.parametrize('method,args', [
    ('_read_compile_receipt', ('knowledge/daily/2026-01-01.md', 'a' * 64)),
    ('_receipt_operation_state', ('knowledge/daily/2026-01-01.md', 'a' * 64)),
    ('_transaction_rows', ('2026-01-01.md',)),
    ('_writer_active', ()),
])
def test_expired_archive_authority_is_not_invalid_or_missing(archive_vault, method, args):
    root, state, _daily = archive_vault
    archiver = DailyArchiver(root, state)
    archiver.deadline = time.monotonic() - 1
    with pytest.raises(TimeoutError):
        getattr(archiver, method)(*args)


def _assert_unlocked_recovery(archiver, source, path, monkeypatch):
    # Recovery checks the caller clock, not an OS scheduling/IO guarantee.
    # The real blocked phase below keeps its original wall-clock assertion.
    now = time.monotonic()
    clock = Mock(wraps=time)
    clock.monotonic.return_value = now
    with monkeypatch.context() as clocks:
        for module in (archive_daily, markdown_transaction, reliable_memory):
            clocks.setattr(module, "time", clock)
        archiver.deadline = now + 2
        assert archiver._receipt_operation_state(source['logical_path'], source['sha256'], path=path) == 'committed'
        clock.monotonic.return_value = archiver.deadline
        with pytest.raises(TimeoutError):
            archiver._receipt_operation_state(source['logical_path'], source['sha256'], path=path)


def test_locked_archive_operation_read_obeys_clock_and_can_recover(archive_vault, monkeypatch):
    root, state, _daily = archive_vault
    archiver = DailyArchiver(root, state)
    path, receipt = _receipt(root)
    source = receipt['source']
    with sqlite3.connect(archiver.coordinator.database_path, isolation_level=None) as blocker:
        blocker.execute('BEGIN EXCLUSIVE')
        started = time.monotonic()
        archiver.deadline = started + 0.04
        with pytest.raises((TimeoutError, sqlite3.OperationalError)):
            archiver._receipt_operation_state(source['logical_path'], source['sha256'], path=path)
        assert time.monotonic() - started < 0.5
        blocker.rollback()
    _assert_unlocked_recovery(archiver, source, path, monkeypatch)


@pytest.mark.parametrize('method', ['_committed_receipt_transaction', '_committed_compile_transaction'])
def test_expired_archive_committed_record_lookup_is_visible(archive_vault, method):
    root, state, _daily = archive_vault
    archiver = DailyArchiver(root, state)
    _path, receipt = _receipt(root)
    archiver.deadline = time.monotonic() - 1
    with pytest.raises(TimeoutError):
        getattr(archiver, method)(receipt)


def test_expired_archive_commit_sequence_does_not_read(archive_vault):
    root, state, _daily = archive_vault
    archiver = DailyArchiver(root, state)
    _path, receipt = _receipt(root)
    transaction = archiver.coordinator._record_for_operation_id(receipt['operation_id'])
    archiver.deadline = time.monotonic() - 1
    with pytest.raises(TimeoutError):
        archiver._commit_sequence(transaction)
