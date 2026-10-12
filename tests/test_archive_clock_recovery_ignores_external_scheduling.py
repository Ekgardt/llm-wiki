"""A recovery-clock check must not assert a host scheduling guarantee."""
from __future__ import annotations

import time
from contextlib import contextmanager
from unittest.mock import Mock

import archive_daily
import markdown_transaction
import pytest
import reliable_memory

from tests import test_archive_receipt_authority_keeps_one_clock as authority_tests
from tests.test_archive_daily_bagit import archive_vault as archive_vault


@pytest.fixture(autouse=True)
def external_scheduling_advance(monkeypatch):
    """Advance the host clock after a real, unblocked connection opens."""
    actual_monotonic = time.monotonic
    advance = [0.0]
    clock = Mock(wraps=time)
    clock.monotonic.side_effect = lambda: actual_monotonic() + advance[0]
    for module in (archive_daily, markdown_transaction, reliable_memory, authority_tests):
        monkeypatch.setattr(module, "time", clock)
    original_archiver = authority_tests.DailyArchiver

    def observed_archiver(*args, **kwargs):
        archiver = original_archiver(*args, **kwargs)
        original_connect = archiver.coordinator._connect

        @contextmanager
        def delayed_connection(*connection_args, **connection_kwargs):
            with original_connect(*connection_args, **connection_kwargs) as database:
                # Longer than the existing 2 s recovery budget, without sleeping.
                # A blocked connection never reaches this point.
                advance[0] += 3.0
                yield database

        monkeypatch.setattr(archiver.coordinator, "_connect", delayed_connection)
        return archiver

    monkeypatch.setattr(authority_tests, "DailyArchiver", observed_archiver)


test_recovery_under_external_scheduling_delay = (
    authority_tests.test_locked_archive_operation_read_obeys_clock_and_can_recover
)
