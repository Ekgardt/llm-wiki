from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import installed_memory_repair as repair
import markdown_transaction
import operational_journal_migration as migration
import pytest
from reliable_memory import OPERATIONAL_JOURNAL_PENDING, OperationalDatabaseContractError

from tests.slow_machine import LONG_TIMEOUT
from tests.test_reliability_v3_adoption import _vault, build_adopted_reliability_v3


@pytest.fixture
def vault(tmp_path):
    root, state = _vault(tmp_path)
    build_adopted_reliability_v3(root, state)
    return root, state


def _assert_mode(root, state, mode):
    adoption = repair.require_reliability_v3_adopted(root=root, state_root=state)
    assert {r['pragmas']['journal_mode'] for r in adoption['databases']} == {mode}
    assert not (state / OPERATIONAL_JOURNAL_PENDING).exists()
    return adoption


def test_roundtrip_preserves_markdown_and_existing_receipts(vault):
    root, state = vault
    coordinator = markdown_transaction.active_markdown_coordinator(root, state)
    before = markdown_transaction._mutate_knowledge(coordinator, "before-migration", {
        root / "knowledge/notes/kept.md": b"# Kept\n",
    }, (), None)
    prepared = coordinator.prepare([
        markdown_transaction.MarkdownChange.create("knowledge/notes/started.md", b"# Started\n")
    ], operation_id="started-before-migration")
    assert migration.migrate(root, state)['status'] == 'completed'
    _assert_mode(root, state, 'wal')
    coordinator = markdown_transaction.active_markdown_coordinator(root, state)
    coordinator.apply(prepared.id)
    same = markdown_transaction._mutate_knowledge(coordinator, "before-migration", {
        root / "knowledge/notes/kept.md": b"# Kept\n",
    }, (), None)
    assert same.id == before.id
    markdown_transaction._mutate_knowledge(coordinator, "after-migration", {
        root / "knowledge/notes/new.md": b"# New\n",
    }, (), None)
    assert migration.migrate(root, state, mode='delete')['status'] == 'completed'
    _assert_mode(root, state, 'delete')
    assert (root / 'knowledge/notes/kept.md').read_bytes() == b"# Kept\n"
    assert (root / 'knowledge/notes/new.md').read_bytes() == b"# New\n"
    assert (root / 'knowledge/notes/started.md').read_bytes() == b"# Started\n"


class Interrupted(RuntimeError):
    pass


@pytest.mark.parametrize('phase', [
    'prepared', 'queue_database', 'coordinator_database', 'queue_tombstone',
    'coordinator_tombstone', 'migration_record', 'adoption_record',
])
@pytest.mark.parametrize('mode', ['wal', 'delete'])
def test_resume_after_each_publication(vault, phase, mode):
    root, state = vault
    _start_mode(root, state, mode)
    registry = migration._registry(state)

    def interrupt(event):
        if event == phase:
            raise Interrupted(event)

    with pytest.raises(Interrupted, match=phase):
        migration.migrate(root, state, mode=mode, emit=interrupt)
    with pytest.raises(repair.ReliabilityV3ValidationError, match='migration is pending'):
        repair.require_reliability_v3_adopted(root=root, state_root=state)
    with pytest.raises(OperationalDatabaseContractError, match='migration is pending'):
        registry.acquire('capture', scope='must-remain-blocked')
    assert migration.migrate(root, state, mode=mode)['status'] == 'completed'
    _assert_mode(root, state, mode)


def _start_mode(root, state, target):
    if target == 'delete':
        migration.migrate(root, state)


def test_live_owner_prevents_any_migration(vault):
    root, state = vault
    registry = migration._registry(state)
    lease = registry.acquire('capture', scope='live-capture')
    try:
        with pytest.raises(RuntimeError, match='quiescence'):
            migration.migrate(root, state)
        _assert_mode(root, state, 'delete')
    finally:
        registry.release(lease)


def test_pending_plan_cannot_be_retargeted(vault):
    root, state = vault
    def interrupt(event):
        raise Interrupted(event)
    with pytest.raises(Interrupted):
        migration.migrate(root, state, emit=interrupt)
    with pytest.raises(ValueError, match='different operation'):
        migration.migrate(root, state, mode='delete')
    assert migration.migrate(root, state)['status'] == 'completed'


def test_unknown_schema_digest_is_still_refused(vault):
    root, state = vault
    path = state / 'run/reliability-v3-adopted.json'
    record = json.loads(path.read_bytes())
    record['schemas']['adoption_schema_sha256'] = '0' * 64
    from reliable_memory import canonical_json_bytes
    path.write_bytes(canonical_json_bytes(record))
    with pytest.raises(repair.ReliabilityV3ValidationError):
        migration.migrate(root, state)


def test_wal_vault_can_be_staged_and_validated(vault, tmp_path):
    import private_vault_backup as backup
    root, state = vault
    migration.migrate(root, state)
    (tmp_path / "staging").mkdir()
    with backup.staged_backup_image(
        root=root, state_root=state, staging_parent=tmp_path / 'staging',
        now=datetime.now(timezone.utc),
    ) as image:
        assert backup.validate_backup_image(image)['database_count'] >= 2


@pytest.mark.parametrize('mode', ['delete', 'wal'])
def test_restored_vault_is_accepted_by_normal_client(vault, tmp_path, mode):
    import time

    import private_vault_backup as backup
    from reliable_memory import sha256_bytes

    root, state = vault
    migration.migrate(root, state, mode=mode)
    staging = tmp_path / 'image'
    staging.mkdir()
    with backup.staged_backup_image(
        root=root, state_root=state, staging_parent=staging,
        now=datetime.now(timezone.utc),
    ) as image:
        digest = sha256_bytes((image / 'manifest.json').read_bytes())
        restored_root, restored_state = tmp_path / 'restored-vault', tmp_path / 'restored-state'
        backup.publish_restored_image(
            image=image, vault_root=restored_root, state_root=restored_state,
            expected_manifest_sha256=digest, deadline=time.monotonic() + LONG_TIMEOUT,
        )
        repair.require_reliability_v3_adopted(root=restored_root, state_root=restored_state)
        coordinator = markdown_transaction.active_markdown_coordinator(restored_root, restored_state)
        markdown_transaction._mutate_knowledge(coordinator, 'restored-write', {
            restored_root / 'knowledge/notes/restored.md': b'# Restored\n',
        }, (), None)
        assert (restored_root / 'knowledge/notes/restored.md').read_bytes() == b'# Restored\n'


@pytest.mark.parametrize('phase', ['queue_database', 'migration_record', 'adoption_record'])
def test_abrupt_process_exit_is_recoverable(vault, monkeypatch, phase):
    import contextlib
    import os
    import sqlite3
    import subprocess
    import sys
    from datetime import timedelta

    root, state = vault
    code = '''import os,sys
from pathlib import Path
from operational_journal_migration import migrate
def crash(event):
    if event == sys.argv[3]:
        os._exit(73)
migrate(Path(sys.argv[1]), Path(sys.argv[2]), emit=crash)
'''
    environment = {**os.environ, 'PYTHONPATH': str(Path(migration.__file__).parent)}
    result = subprocess.run([sys.executable, '-c', code, str(root), str(state), phase],
                            env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 73, result.stderr
    with contextlib.closing(sqlite3.connect(state / 'run/markdown-transactions-v3.sqlite3')) as db:
        expires = db.execute('SELECT expires_at FROM maintenance_owners').fetchone()[0]
    # Advance the test clock past the actual retained lease; never sleep or
    # weaken the production rule that only expired AND proven-dead owners yield.
    after_expiry = datetime.fromisoformat(expires.replace('Z', '+00:00')) + timedelta(microseconds=1)
    original = migration._registry
    def recovered_registry(state):
        registry = original(state)
        registry._clock = lambda: after_expiry
        return registry
    monkeypatch.setattr(migration, '_registry', recovered_registry)
    assert migration.migrate(root, state)['status'] == 'completed'
    _assert_mode(root, state, 'wal')


def test_cached_queue_cannot_write_during_pending_migration(vault):
    import memory_queue

    root, state = vault
    queue = memory_queue.active_memory_queue(root, state)
    task_id = queue.enqueue('query', 1, {'prompt': 'retain me'}, dedupe_key='before')
    def interrupt(event):
        raise Interrupted(event)
    with pytest.raises(Interrupted):
        migration.migrate(root, state, emit=interrupt)
    with pytest.raises(OperationalDatabaseContractError, match='migration is pending'):
        queue.enqueue('query', 1, {'prompt': 'must wait'})
    migration.migrate(root, state)
    resumed = memory_queue.active_memory_queue(root, state)
    assert resumed.enqueue('query', 1, {'prompt': 'retain me'}, dedupe_key='before') == task_id
    assert resumed.enqueue('query', 1, {'prompt': 'after'}) != task_id
