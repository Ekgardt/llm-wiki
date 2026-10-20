"""A newly initialized vault can compile without an upgrade of its empty queue."""
import sqlite3

import compile_progress
import installed_memory_repair as repair
import memory_queue
import pytest

from tests.adopted_vault import adopt


def test_new_install_is_current_and_repeated_adoption_is_read_only(tmp_path):
    root, state = adopt(tmp_path)
    report = repair.inspect_installed_vault(root=root, state_root=state)
    assert report['details']['compile_progress'] == 'upgraded'
    path = state / 'run/queue-v3.sqlite3'
    with sqlite3.connect(path) as database:
        assert database.execute('pragma user_version').fetchone()[0] == compile_progress.QUEUE_SCHEMA_VERSION
    before = path.read_bytes()
    again = repair.repair_installed_vault(root=root, state_root=state,
        adopt_ownership_v3=True, confirm_all_agents_stopped=True)
    assert again['overall_status'] == 'ok'
    assert path.read_bytes() == before


def test_new_install_records_compile_progress_without_schema_patching(tmp_path):
    root, state = adopt(tmp_path)
    queue = memory_queue.active_memory_queue(root, state)
    queue.enqueue('compile', 1, {'input': 'fresh-install-evidence'})
    lease = queue.claim('fresh-install-test')
    event = compile_progress.CompileEvent(
        work_id='a' * 64, task_id=lease.id, sequence=0, source_digest='b' * 64,
        stage='pending', outcome='ready', details={})
    queue.record_compile_event(lease, event)
    assert queue.get_compile_event(event.work_id) == event
    assert queue.get(lease.id).state == 'leased'


def test_current_candidate_refuses_active_database_before_initialization(tmp_path):
    import compile_progress_migration as migration
    import pytest

    root, state = adopt(tmp_path)
    path = state / 'run/queue-v3.sqlite3'
    before = path.read_bytes()
    with pytest.raises(ValueError, match='active queue'):
        migration.initialize_fresh_candidate(path, state_root=state)
    assert path.read_bytes() == before


def test_populated_unpublished_candidate_is_not_a_fresh_install(tmp_path):
    import compile_progress_migration as migration
    import pytest

    root, state = adopt(tmp_path)
    queue = memory_queue.active_memory_queue(root, state)
    queue.enqueue('compile', 1, {'retained': 'work'})
    candidate = state / 'run/candidate.sqlite3'
    with sqlite3.connect(candidate) as copied, queue.connection() as source:
        source.backup(copied)
    candidate.chmod(0o600)
    before = candidate.read_bytes()
    with pytest.raises(memory_queue.OperationalDatabaseContractError, match='existing rows'):
        migration.initialize_fresh_candidate(candidate, state_root=state)
    assert candidate.read_bytes() == before


def test_fresh_adoption_is_included_in_a_valid_backup(tmp_path):
    import private_vault_backup as backup

    root, state = adopt(tmp_path)
    image = tmp_path / 'backup'
    image.mkdir(mode=0o700)
    with backup.staged_backup_image(root=root, state_root=state, staging_parent=image):
        assert backup.validate_backup_image(image)['database_count'] == 2


def test_fresh_record_cannot_claim_old_schema_digests(tmp_path):
    import json

    import pytest
    from reliable_memory import canonical_json_bytes

    root, state = adopt(tmp_path)
    path = state / 'run/reliability-v3-adopted.json'
    record = json.loads(path.read_bytes())
    record['schemas'] = repair._expected_schema_digests()
    path.write_bytes(canonical_json_bytes(record))
    with pytest.raises(repair.ReliabilityV3ValidationError, match='schema digests changed'):
        repair.require_reliability_v3_admission(root=root, state_root=state)


@pytest.mark.parametrize('boundary', ['candidate', 'publication', 'adoption'])
def test_process_death_during_fresh_adoption_is_resumable(tmp_path, boundary):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from tests.slow_machine import SHORT_TIMEOUT
    from tests.test_reliability_v3_adoption import _vault

    root, state = _vault(tmp_path)
    script = '''
import os,sys
from pathlib import Path
import installed_memory_repair as repair
import compile_progress_migration as migration
boundary,root,state = sys.argv[1:]
module,name = {'candidate': (migration,'initialize_fresh_candidate'),
               'publication': (repair,'_publish_candidate'),
               'adoption': (repair,'_publish_immutable_record')}[boundary]
original=getattr(module,name)
def interrupted(*args,**kwargs):
    result=original(*args,**kwargs)
    if boundary != 'adoption' or kwargs.get('schema') == repair._FRESH_ADOPTION_SCHEMA:
        os._exit(71)
    return result
setattr(module,name,interrupted)
repair._apply_reliability_v3_adoption(root=Path(root),state_root=Path(state))
'''
    env = dict(os.environ, PYTHONPATH=str(Path(repair.__file__).parent))
    result = subprocess.run([sys.executable, '-c', script, boundary, str(root), str(state)],
                            env=env, capture_output=True, timeout=SHORT_TIMEOUT)
    assert result.returncode == 71, result.stderr.decode()
    report = repair.repair_installed_vault(root=root, state_root=state,
        adopt_ownership_v3=True, confirm_all_agents_stopped=True)
    assert report['overall_status'] == 'ok', report
    assert repair.inspect_installed_vault(root=root, state_root=state)['details']['compile_progress'] == 'upgraded'
    assert memory_queue.active_memory_queue(root, state).enqueue('compile', 1, {'resumed': boundary})


def test_interrupted_old_plan_keeps_its_original_schema(tmp_path, monkeypatch):
    from tests.test_reliability_v3_adoption import _vault

    root, state = _vault(tmp_path)

    def stop_before_candidate(*args):
        raise RuntimeError('interrupted old installation')

    with monkeypatch.context() as patch:
        patch.setattr(repair, '_initial_schema_digests', lambda source: repair._expected_schema_digests())
        patch.setattr(repair, '_prepare_candidate', stop_before_candidate)
        report = repair.repair_installed_vault(root=root, state_root=state,
            adopt_ownership_v3=True, confirm_all_agents_stopped=True)
    assert report['overall_status'] == 'error'
    report = repair.repair_installed_vault(root=root, state_root=state,
        adopt_ownership_v3=True, confirm_all_agents_stopped=True)
    assert report['overall_status'] == 'ok', report
    assert repair.inspect_installed_vault(root=root, state_root=state)['details']['compile_progress'] == 'upgrade-required'
    with sqlite3.connect(state / 'run/queue-v3.sqlite3') as database:
        assert database.execute('pragma user_version').fetchone()[0] == 3


@pytest.mark.parametrize('platform', ['bash', 'pwsh'])
def test_installers_accept_a_real_fresh_queue_without_backup_flags(tmp_path, platform):
    from tests.test_compile_progress_installers import _run_slice

    root, state = adopt(tmp_path)
    result, calls = _run_slice(tmp_path, platform, state, '', False, TEST_RUN_REAL='1',
                              LLM_WIKI_ROOT=str(root), LLM_WIKI_STATE_ROOT=str(state))
    assert 'WARNING:0' in result.stdout
    assert not [call for call in calls if '--apply' in call]


@pytest.mark.parametrize('database,wrong_version', [('queue', 3), ('coordinator', 4)])
def test_fresh_record_pins_each_component_version(tmp_path, database, wrong_version):
    import json

    from reliable_memory import canonical_json_bytes

    root, state = adopt(tmp_path)
    path = state / 'run/reliability-v3-adopted.json'
    record = json.loads(path.read_bytes())
    entry = next(item for item in record['databases'] if item['database'] == database)
    entry['user_version'] = wrong_version
    path.write_bytes(canonical_json_bytes(record))
    with pytest.raises(repair.ReliabilityV3ValidationError):
        repair.require_reliability_v3_admission(root=root, state_root=state)


def test_explicit_upgrade_of_fresh_install_reports_no_fictitious_backup(tmp_path):
    import json

    from tests.test_compile_progress_repair_cli import _arguments
    from tests.test_repair_installed_memory import _run

    root, state = adopt(tmp_path)
    image = tmp_path / 'unused-image'
    image.mkdir(mode=0o700)
    result = _run(*_arguments(root, state, image))
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report['details']['compile_progress'] == 'upgraded'
    assert report['actions'] == []
    assert 'backup_manifest_sha256' not in report['details']
    assert list(image.iterdir()) == []


def test_fresh_candidate_outside_state_is_refused_without_creating_it(tmp_path):
    import compile_progress_migration as migration

    root, state = adopt(tmp_path)
    outside = tmp_path / 'outside.sqlite3'
    with pytest.raises((PermissionError, ValueError), match='outside'):
        migration.initialize_fresh_candidate(outside, state_root=state)
    assert not outside.exists()
