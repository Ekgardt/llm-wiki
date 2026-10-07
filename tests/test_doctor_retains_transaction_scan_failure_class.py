"""Incomplete transaction inspection retains its cause and deletion refusal."""
import sqlite3
import time
from datetime import datetime, timezone

import doctor
import pytest

from tests.adopted_vault import adopt


@pytest.mark.parametrize('error_type', [TimeoutError, PermissionError, sqlite3.OperationalError, ValueError])
def test_transaction_scan_failure_keeps_its_class_without_exception_content(tmp_path, monkeypatch, error_type):
    root, state = adopt(tmp_path)
    private_detail = 'private source content must not enter the health response'

    def fail_scan(*args, **kwargs):
        raise error_type(private_detail)

    monkeypatch.setattr(doctor, '_scan_transaction_database', fail_scan)
    result = doctor._transaction_check(state, datetime.now(timezone.utc), time.monotonic() + 30, vault_root=root)
    assert result['details']['read_error_class'] == error_type.__name__
    assert result['status'] == 'error'
    assert result['details']['read_error'] is True
    assert 'transaction_state_unreadable' in result['details']['deletion_codes']
    assert private_detail not in str(result)
