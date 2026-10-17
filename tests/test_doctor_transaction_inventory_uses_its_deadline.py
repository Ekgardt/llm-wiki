"""A complete transaction inventory is bounded by time, not record count."""
import time

import doctor
import pytest

from tests.slow_machine import LONG_TIMEOUT


def test_inventory_reads_more_than_the_old_entry_ceiling(tmp_path):
    directory = tmp_path / 'run/transactions'
    directory.mkdir(parents=True)
    expected = {f'transaction_{number}' for number in range(doctor.MAX_RUNTIME_ENTRIES + 1)}
    for identifier in expected:
        (directory / identifier).mkdir()
    identifiers, incomplete = doctor._transaction_artifacts(tmp_path, time.monotonic() + LONG_TIMEOUT)
    assert incomplete is False
    assert identifiers == expected


def test_expired_inventory_is_never_complete(tmp_path):
    (tmp_path / 'run/transactions').mkdir(parents=True)
    identifiers, incomplete = doctor._transaction_artifacts(tmp_path, time.monotonic() - 1)
    assert incomplete is True
    assert identifiers == set()


@pytest.mark.parametrize('kind', ['file', 'symlink', 'invalid-name'])
def test_unsafe_entry_still_refuses_complete_inventory(tmp_path, kind):
    directory = tmp_path / 'run/transactions'
    directory.mkdir(parents=True)
    entry = directory / 'transaction'
    actions = {'file': lambda: entry.write_bytes(b'unsafe'), 'symlink': lambda: entry.symlink_to(tmp_path, target_is_directory=True), 'invalid-name': lambda: (directory / 'INVALID').mkdir()}
    actions[kind]()
    _, incomplete = doctor._transaction_artifacts(tmp_path, time.monotonic() + LONG_TIMEOUT)
    assert incomplete is True


def test_staged_prune_is_not_a_transaction_or_damage(tmp_path):
    directory = tmp_path / 'run/transactions'
    directory.mkdir(parents=True)
    (directory / '.old.pruning-verified').mkdir()
    (directory / 'retained').mkdir()
    identifiers, incomplete = doctor._transaction_artifacts(tmp_path, time.monotonic() + LONG_TIMEOUT)
    assert (identifiers, incomplete) == ({'retained'}, False)


@pytest.mark.parametrize('present', [True, False])
def test_complete_inventory_does_not_repeat_filesystem_inspection(tmp_path, monkeypatch, present):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    row = {'id': 'retained', 'artifacts_pruned_at': None, 'updated_at': now.isoformat()}
    calls = []

    def unexpected_stat(*args):
        calls.append(args)
        return 'unsafe', None

    monkeypatch.setattr(doctor, '_safe_kind', unexpected_stat)
    artifacts = {'retained'} if present else set()
    assert doctor._undo_artifact_retained(row, 'committed', now, tmp_path, artifacts=artifacts) is present
    assert calls == []


@pytest.mark.parametrize('kind,expected', [('directory', True), ('symlink', False), ('missing', False)])
def test_incomplete_inventory_still_inspects_undo_directory(tmp_path, monkeypatch, kind, expected):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    row = {'id': 'retained', 'artifacts_pruned_at': None, 'updated_at': now.isoformat()}
    calls = []

    def inspect(*args):
        calls.append(args)
        return kind, None

    monkeypatch.setattr(doctor, '_safe_kind', inspect)
    assert doctor._undo_artifact_retained(row, 'committed', now, tmp_path, artifacts=None) is expected
    assert len(calls) == 1
