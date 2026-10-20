"""Offline cutover retains a real backup and resumes interrupted schema work."""
from __future__ import annotations

import errno
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import compile_progress_migration as migration
import installed_memory_repair as repair
import memory_queue
import operational_ownership as ownership
import private_vault_backup as backup
import pytest
from iso_time import parse_instant
from reliable_memory import OperationalDatabaseContractError

from tests.adopted_vault import adopt_legacy as adopt


def _upgrade(root, state, image, **kwargs):
    return migration.upgrade_offline(root=root, state_root=state, backup_image=image,
                                     confirm_all_agents_stopped=True, **kwargs)


def _interrupt_after_intent(event):
    if event == 'after_intent':
        raise RuntimeError('injected interruption')


def _pending(tmp_path):
    root, state = adopt(tmp_path)
    image = tmp_path / 'backup'
    image.mkdir(mode=0o700)
    with pytest.raises(RuntimeError, match='injected'):
        _upgrade(root, state, image, killpoint=_interrupt_after_intent)
    return root, state, image


def test_offline_upgrade_preserves_work_and_verified_backup(tmp_path):
    root, state = adopt(tmp_path)
    queue = memory_queue.active_memory_queue(root, state)
    task = queue.enqueue('compile', 1, {'input': 'retained'})
    inode = queue.db_path.stat().st_ino
    image = tmp_path / 'backup'
    image.mkdir(mode=0o700)
    record = migration.upgrade_offline(root=root, state_root=state, backup_image=image,
                                       confirm_all_agents_stopped=True)
    assert record['schema_version'] == 'reliability-v3-adoption/v2'
    assert queue.db_path.stat().st_ino == inode
    assert memory_queue.active_memory_queue(root, state).get(task).payload == {'input': 'retained'}
    assert repair.require_reliability_v3_adopted(root=root, state_root=state) == record
    assert backup.validate_backup_image(image)['database_count'] == 2
    previous = json.loads((image / 'state/run/reliability-v3-adopted.json').read_bytes())
    assert record['previous'] == previous


@pytest.mark.parametrize('point', ['after_intent', 'after_statement:0', 'after_statement:1',
                                 'after_statement:2', 'after_version', 'before_commit', 'after_commit'])
def test_abrupt_process_death_recovers(tmp_path, point, monkeypatch):
    root, state = adopt(tmp_path)
    image = tmp_path / 'backup'
    image.mkdir(mode=0o700)
    code = '''
import os, sys
from pathlib import Path
from unittest.mock import patch
import compile_progress_migration as migration
root, state, image, point = sys.argv[1:]
def die(event):
    if event == point:
        os._exit(17)
migration.upgrade_offline(root=Path(root), state_root=Path(state), backup_image=Path(image),
                         confirm_all_agents_stopped=True, killpoint=die)
'''
    env = dict(os.environ, PYTHONPATH=str(Path(migration.__file__).parent))
    result = subprocess.run([sys.executable, '-c', code, str(root), str(state), str(image), point],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 17, result.stderr
    with pytest.raises(backup.BackupError, match='owner_unavailable'):
        _upgrade(root, state, image)
    _advance_past_owner_expiry(state, monkeypatch)
    record = _upgrade(root, state, image)
    assert repair.require_reliability_v3_adopted(root=root, state_root=state) == record


def _advance_past_owner_expiry(state, monkeypatch):
    with closing(sqlite3.connect(state / 'run/markdown-transactions-v3.sqlite3')) as database:
        expiry = database.execute('SELECT MAX(expires_at) FROM maintenance_owners').fetchone()[0]
    instant = parse_instant(expiry) + timedelta(microseconds=1)
    original = ownership.OwnershipRegistry._from_adopted_database

    def open_with_clock(cls, *args, **kwargs):
        return original(*args, **dict(kwargs, clock=lambda: instant))

    monkeypatch.setattr(ownership.OwnershipRegistry, '_from_adopted_database', classmethod(open_with_clock))


def test_pending_intent_blocks_new_client(tmp_path):
    root, state, _image = _pending(tmp_path)
    with pytest.raises(repair.ReliabilityV3ValidationError):
        memory_queue.active_memory_queue(root, state)


def test_changed_backup_is_not_used_for_recovery(tmp_path):
    root, state, image = _pending(tmp_path)
    (image / 'state/run/queue-v3.sqlite3').write_bytes(b'corrupt')
    with pytest.raises(backup.BackupError):
        _upgrade(root, state, image)
    with closing(sqlite3.connect(state / 'run/queue-v3.sqlite3')) as database:
        assert database.execute('PRAGMA user_version').fetchone()[0] == 3


def test_changed_preimage_is_not_migrated(tmp_path):
    root, state, image = _pending(tmp_path)
    with closing(sqlite3.connect(state / 'run/queue-v3.sqlite3')) as database:
        database.execute('PRAGMA application_id=123')
    with pytest.raises(OperationalDatabaseContractError, match='not a queue'):
        _upgrade(root, state, image)


def test_completed_retry_preserves_new_work(tmp_path):
    root, state, image = _pending(tmp_path)
    first = _upgrade(root, state, image)
    queue = memory_queue.active_memory_queue(root, state)
    task = queue.enqueue('compile', 1, {'after': 'migration'})
    assert _upgrade(root, state, image) == first
    assert queue.get(task).payload == {'after': 'migration'}


def test_live_owner_blocks_even_with_confirmation(tmp_path):
    root, state = adopt(tmp_path)
    image = tmp_path / 'backup'
    image.mkdir(mode=0o700)
    queue = memory_queue.active_memory_queue(root, state)
    with queue.queue_owner(role='queue-worker', scope='test-live-owner'):
        with pytest.raises(backup.BackupError, match='quiescence'):
            _upgrade(root, state, image)
    assert list(image.iterdir()) == []


def test_offline_confirmation_is_required(tmp_path):
    with pytest.raises(ValueError, match='all agents stopped'):
        migration.upgrade_offline(root=tmp_path, state_root=tmp_path, backup_image=tmp_path)


def test_upgrade_supports_the_installed_single_directory_layout(tmp_path):
    root = tmp_path / 'vault'
    (root / 'scripts').mkdir(parents=True)
    (root / 'scripts/integration_adapter.py').write_bytes(
        Path(migration.__file__).with_name('integration_adapter.py').read_bytes())
    with patch("installed_memory_repair._initial_schema_digests",
               return_value=repair._expected_schema_digests()):
        repair.repair_installed_vault(root=root, state_root=root, adopt_ownership_v3=True,
                                      confirm_all_agents_stopped=True)
    image = tmp_path / 'backup'
    image.mkdir(mode=0o700)
    record = _upgrade(root, root, image)
    assert repair.require_reliability_v3_adopted(root=root, state_root=root) == record
    assert backup.validate_backup_image(image)['database_count'] == 2


def test_valid_but_changed_queue_preimage_blocks_resume(tmp_path):
    root, state = adopt(tmp_path)
    old_client = memory_queue.active_memory_queue(root, state)
    image = tmp_path / 'backup'
    image.mkdir(mode=0o700)
    with pytest.raises(RuntimeError, match='injected'):
        _upgrade(root, state, image, killpoint=_interrupt_after_intent)
    # Deliberately violate the offline precondition using an old held client.
    old_client.enqueue('compile', 1, {'unexpected': 'writer'})
    with pytest.raises(ValueError, match='preimage changed'):
        _upgrade(root, state, image)


def test_backup_disk_failure_preserves_original_admission(tmp_path, monkeypatch):
    root, state = adopt(tmp_path)
    before = (state / 'run/reliability-v3-adopted.json').read_bytes()
    image = tmp_path / 'backup'
    image.mkdir(mode=0o700)

    def full_disk(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, 'injected full disk')

    monkeypatch.setattr(backup, '_copy_entries', full_disk)
    with pytest.raises(OSError, match='full disk'):
        _upgrade(root, state, image)
    assert (state / 'run/reliability-v3-adopted.json').read_bytes() == before
    assert repair.require_reliability_v3_adopted(root=root, state_root=state)['schema_version'].endswith('/v1')
    assert image.exists()


def test_owner_loss_before_commit_rolls_back_schema(tmp_path):
    root, state = adopt(tmp_path)
    image = tmp_path / 'backup'
    image.mkdir(mode=0o700)

    def lose_owner(event):
        if event == 'before_commit':
            with closing(sqlite3.connect(state / 'run/markdown-transactions-v3.sqlite3')) as database:
                database.execute('DELETE FROM maintenance_owners')
                database.commit()

    with pytest.raises(ownership.OperationalOwnershipError, match='owner_fence_lost'):
        _upgrade(root, state, image, killpoint=lose_owner)
    with closing(sqlite3.connect(state / 'run/queue-v3.sqlite3')) as database:
        assert database.execute('PRAGMA user_version').fetchone()[0] == 3
    assert _upgrade(root, state, image)['schema_version'].endswith('/v2')


def test_complete_backup_before_intent_failure_is_retained(tmp_path):
    root, state = adopt(tmp_path)
    image = tmp_path / 'backup'
    image.mkdir(mode=0o700)

    def interrupt(event):
        if event == 'after_backup':
            raise RuntimeError('injected interruption')

    with pytest.raises(RuntimeError, match='injected'):
        _upgrade(root, state, image, killpoint=interrupt)
    assert backup.validate_backup_image(image)['database_count'] == 2
    with pytest.raises(backup.BackupError, match='staging_not_empty'):
        _upgrade(root, state, image)
    fresh_image = tmp_path / 'new-backup'
    fresh_image.mkdir(mode=0o700)
    assert _upgrade(root, state, fresh_image)['schema_version'].endswith('/v2')


@pytest.mark.parametrize('point', ['after_intent', 'after_statement:0', 'before_commit', 'after_commit'])
def test_interrupted_upgrade_can_resume(tmp_path, point):
    root, state = adopt(tmp_path)
    image = tmp_path / 'backup'
    image.mkdir(mode=0o700)

    def interrupt(event):
        if event == point:
            raise RuntimeError('injected interruption')

    with pytest.raises(RuntimeError, match='injected'):
        migration.upgrade_offline(root=root, state_root=state, backup_image=image,
                                  confirm_all_agents_stopped=True, killpoint=interrupt)
    record = migration.upgrade_offline(root=root, state_root=state, backup_image=image,
                                       confirm_all_agents_stopped=True)
    assert repair.require_reliability_v3_adopted(root=root, state_root=state) == record
