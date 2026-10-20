"""A restart must not buy the same uncertain model call twice."""
from dataclasses import replace

import compile_progress as progress
import memory_queue
import pytest
from context_budget import TokenUsage
from llm_client import BackendResponse
from reliable_memory import sha256_bytes

from tests.test_compile_progress_admission import upgraded as upgraded


def _source_scope(path, content):
    return progress.make_source_scope([{
        'logical_path': path, 'byte_start': 0, 'byte_end': len(content),
        'sha256': sha256_bytes(content)}])


@pytest.fixture
def work(upgraded):
    root, state, _ = upgraded
    queue = memory_queue.active_memory_queue(root, state)
    lease = queue.claim('compile-worker')
    source = b'verified input snapshot'
    scope = _source_scope('knowledge/daily/2026-10-20.md', source)
    first = progress.CompileEvent('c' * 64, lease.id, 0, 'd' * 64, 'pending', 'ready',
                                 {'source_scope': scope})
    queue.record_compile_event(lease, first)
    checkpoint = queue.save_compile_artifact(
        lease, replace(first, sequence=1, stage='snapshot_ready'), source,
    )
    return queue, lease, checkpoint


def _run(work, invoke, **options):
    from compile_controller import run_model_call

    queue, lease, checkpoint = work
    return run_model_call(queue, lease, checkpoint, request_digest='e' * 64, invoke=invoke, **options)


def _answer():
    return BackendResponse('answer', TokenUsage(input_tokens=123, output_tokens=17))


def _must_not_call():
    pytest.fail('recovery must not call the model again')


def test_saved_answer_and_usage_survive_controller_restart(work):
    assert _run(work, _answer) == _answer()
    assert _run(work, _must_not_call) == _answer()
    queue, lease, _ = work
    assert queue.get(lease.id).state == 'leased'
    assert queue.get(lease.id).result_reference is None


def test_unknown_provider_outcome_never_calls_again(work):
    from compile_controller import ProviderOutcomeUnknown

    def interrupted():
        raise SystemExit('process stopped during provider call')

    with pytest.raises(SystemExit):
        _run(work, interrupted)
    with pytest.raises(ProviderOutcomeUnknown):
        _run(work, _must_not_call)


def test_answer_file_recovers_when_event_commit_failed(work, monkeypatch):
    queue, _, _ = work
    original = queue.record_compile_event

    def fail_answer_commit(lease, event):
        if event.details.get('model_call', {}).get('phase') == 'answer':
            raise RuntimeError('interrupted before answer event')
        return original(lease, event)

    with monkeypatch.context() as patch:
        patch.setattr(queue, 'record_compile_event', fail_answer_commit)
        with pytest.raises(RuntimeError):
            _run(work, _answer)
    assert _run(work, _must_not_call) == _answer()


def test_unknown_call_is_durably_blocked_in_delivery(work):
    from compile_controller import ProviderOutcomeUnknown

    def timeout():
        raise TimeoutError('provider may have accepted the call')

    with pytest.raises(ProviderOutcomeUnknown):
        _run(work, timeout)
    queue, lease, _ = work
    assert queue.get(lease.id).state == 'blocked'
    assert queue.get(lease.id).blocked_capability == 'provider_outcome_unknown'
    queue.unblock(lease.id)
    replacement = queue.claim('another-worker')
    with pytest.raises(ProviderOutcomeUnknown):
        _run((queue, replacement, work[2]), _must_not_call)


def test_changed_request_cannot_turn_unknown_dispatch_into_new_expense(work):
    from compile_controller import run_model_call

    def interrupted():
        raise SystemExit()

    with pytest.raises(SystemExit):
        _run(work, interrupted)
    queue, lease, checkpoint = work
    with pytest.raises(progress.ProgressConflict):
        run_model_call(queue, lease, checkpoint, request_digest='f' * 64, invoke=_must_not_call)


def test_stale_lease_cannot_dispatch(work):
    queue, lease, checkpoint = work
    with pytest.raises(memory_queue.LeaseFenceError):
        _run((queue, replace(lease, token='stale'), checkpoint), _must_not_call)
    assert queue.get_compile_event(checkpoint.work_id) == checkpoint


def test_unrelated_task_cannot_take_over_compile_work(work):
    queue, _, checkpoint = work
    queue.enqueue('compile', 1, {'daily': 'unrelated'})
    other = queue.claim('other-worker')
    with pytest.raises(progress.ProgressConflict):
        _run((queue, other, checkpoint), _must_not_call)


def test_redrive_reuses_answer_and_usage_without_new_call(work):
    queue, lease, checkpoint = work
    assert _run(work, _answer) == _answer()
    queue.fail(lease, memory_queue.QueueFailure('handler_failed', permanent=True))
    child_id = queue.redrive(lease.id)
    child = queue.claim('redrive-worker')
    assert child.id == child_id
    assert _run((queue, child, checkpoint), _must_not_call) == _answer()


def test_unknown_usage_stays_unknown_after_recovery(work):
    reply = BackendResponse('answer without usage')
    assert _run(work, lambda: reply) == reply
    recovered = _run(work, _must_not_call)
    assert recovered.usage.input_tokens is None
    assert recovered.usage.estimated_cost is None
    assert recovered.usage.cost_kind == 'unknown'


def test_unvalidated_answer_cannot_authorize_another_call(work):
    queue, lease, checkpoint = work
    _run(work, _answer)
    answer = queue.get_compile_event(checkpoint.work_id)
    with pytest.raises(progress.ProgressConflict, match='semantic validation'):
        _run((queue, lease, answer), _must_not_call)


def test_corrupt_orphan_is_refused_before_recording_an_answer(work, monkeypatch):
    queue, _, checkpoint = work
    original = queue.record_compile_event

    def stop_after_file(lease, event):
        if event.details.get('model_call', {}).get('phase') == 'answer':
            raise RuntimeError('stop before event')
        return original(lease, event)

    with monkeypatch.context() as patch:
        patch.setattr(queue, 'record_compile_event', stop_after_file)
        with pytest.raises(RuntimeError):
            _run(work, _answer)
    dispatch = queue.get_compile_event(checkpoint.work_id)
    identity = f'compile:{checkpoint.work_id}:{dispatch.sequence + 1}'
    path = queue.results_dir / (memory_queue.sha256_bytes(identity.encode()) + '.result')
    path.write_bytes(b'{"wrong":"answer"}')
    with pytest.raises(progress.ProgressCorrupt):
        _run(work, _must_not_call)
    assert queue.get_compile_event(checkpoint.work_id) == dispatch


def test_missing_saved_answer_is_corruption_not_permission_to_regenerate(work):
    queue, _, checkpoint = work
    _run(work, _answer)
    answer = queue.get_compile_event(checkpoint.work_id)
    (queue.state_root / answer.details['artifact']['reference']).unlink()
    with pytest.raises(progress.ProgressCorrupt):
        _run(work, _must_not_call)


def test_lease_loss_after_provider_call_cannot_record_false_completion(work, monkeypatch):
    from datetime import timedelta

    queue, lease, checkpoint = work

    def slow_answer():
        monkeypatch.setattr(memory_queue, '_utc_now', lambda: lease.expires_at + timedelta(seconds=1))
        return _answer()

    with pytest.raises(memory_queue.LeaseFenceError):
        _run(work, slow_answer)
    assert queue.get_compile_event(checkpoint.work_id).outcome == 'running'


_CRASH_CHILD = '''
import json, os, sys
from pathlib import Path
from dataclasses import asdict
import compile_progress, compile_controller, memory_queue
from iso_time import parse_instant
from llm_client import BackendResponse
queue = memory_queue.active_memory_queue(Path(sys.argv[1]), Path(sys.argv[2]))
lease_data = json.loads(sys.argv[3])
lease_data['expires_at'] = parse_instant(lease_data['expires_at'])
lease = memory_queue.QueueLease(**lease_data)
checkpoint = compile_progress.CompileEvent(**json.loads(sys.argv[4]))
phase = sys.argv[5]
original = queue.record_compile_event

def record(owner, event):
    if phase == 'answer' and event.details.get('model_call', {}).get('phase') == 'answer':
        os._exit(17)
    return original(owner, event)

def answer():
    if phase == 'dispatch':
        os._exit(17)
    return BackendResponse('durable child answer')

queue.record_compile_event = record
compile_controller.run_model_call(queue, lease, checkpoint, request_digest='e' * 64, invoke=answer)
'''


@pytest.mark.parametrize('phase', ['dispatch', 'answer'])
def test_abrupt_process_exit_recovers_without_another_model_call(work, upgraded, phase):
    import json
    import os
    import subprocess
    import sys
    from dataclasses import asdict
    from pathlib import Path

    from compile_controller import ProviderOutcomeUnknown

    root, state, _ = upgraded
    _, lease, checkpoint = work
    child = subprocess.run(
        [sys.executable, '-c', _CRASH_CHILD, str(root), str(state),
         json.dumps(asdict(lease), default=str), json.dumps(asdict(checkpoint)), phase],
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'scripts')},
        capture_output=True, text=True, check=False,
    )
    assert child.returncode == 17, child.stderr
    if phase == 'dispatch':
        with pytest.raises(ProviderOutcomeUnknown):
            _run(work, _must_not_call)
        return
    assert _run(work, _must_not_call) == BackendResponse('durable child answer')


def test_empty_answer_preserves_known_cost_without_becoming_a_validated_plan(work):
    reply = BackendResponse('', TokenUsage(input_tokens=40, output_tokens=0))
    assert _run(work, lambda: reply) == reply
    assert _run(work, _must_not_call) == reply
    queue, _, checkpoint = work
    assert queue.get_compile_event(checkpoint.work_id).stage == 'snapshot_ready'


def test_redrive_of_uncertain_call_stays_blocked(work):
    from compile_controller import ProviderOutcomeUnknown

    def interrupted():
        raise SystemExit()

    with pytest.raises(SystemExit):
        _run(work, interrupted)
    queue, lease, checkpoint = work
    queue.fail(lease, memory_queue.QueueFailure('worker_failed', permanent=True))
    child_id = queue.redrive(lease.id)
    child = queue.claim('redrive-worker')
    assert child.id == child_id
    with pytest.raises(ProviderOutcomeUnknown):
        _run((queue, child, checkpoint), _must_not_call)
    assert queue.get(child_id).state == 'blocked'


def test_transport_starts_only_after_committed_reservation_and_without_sql_lock(work):
    queue, _, checkpoint = work

    def invoke():
        current = queue.get_compile_event(checkpoint.work_id)
        assert current.outcome == 'running'
        assert current.details['model_call']['phase'] == 'dispatch'
        with queue.connection() as database:
            database.execute('BEGIN IMMEDIATE')
            database.rollback()
        return _answer()

    assert _run(work, invoke) == _answer()


def test_reservation_failure_never_invokes_transport(work, monkeypatch):
    queue, _, checkpoint = work

    def failed_write(*args):
        raise OSError('reservation could not commit')

    monkeypatch.setattr(queue, 'record_compile_event', failed_write)
    with pytest.raises(OSError):
        _run(work, _must_not_call)
    assert queue.get_compile_event(checkpoint.work_id) == checkpoint


def test_missing_checkpoint_is_a_conflict_not_a_new_dispatch(work):
    queue, lease, checkpoint = work
    with pytest.raises(progress.ProgressConflict, match='not recorded'):
        _run((queue, lease, replace(checkpoint, work_id='f' * 64)), _must_not_call)


@pytest.mark.parametrize('field', ['work_id', 'source_digest', 'sequence'])
def test_answer_for_another_call_cannot_be_adopted_from_an_orphan(work, monkeypatch, field):
    import json

    from reliable_memory import canonical_json_bytes

    queue, _, checkpoint = work
    original = queue.record_compile_event

    def stop_after_file(lease, event):
        if event.details.get('model_call', {}).get('phase') == 'answer':
            raise RuntimeError('stop before event')
        return original(lease, event)

    with monkeypatch.context() as patch:
        patch.setattr(queue, 'record_compile_event', stop_after_file)
        with pytest.raises(RuntimeError):
            _run(work, _answer)
    dispatch = queue.get_compile_event(checkpoint.work_id)
    identity = f'compile:{checkpoint.work_id}:{dispatch.sequence + 1}'
    path = queue.results_dir / (memory_queue.sha256_bytes(identity.encode()) + '.result')
    envelope = json.loads(path.read_bytes())
    envelope['dispatch'][field] = 'different'
    path.write_bytes(canonical_json_bytes(envelope))
    with pytest.raises(progress.ProgressCorrupt):
        _run(work, _must_not_call)
    assert queue.get_compile_event(checkpoint.work_id) == dispatch


def test_explicit_repeat_preserves_unknown_dispatch_and_reuses_checkpoint(work):
    from compile_controller import authorize_model_repeat

    queue, lease, checkpoint = work
    def interrupted():
        raise SystemExit()

    with pytest.raises(SystemExit):
        _run(work, interrupted)
    dispatch = queue.get_compile_event(checkpoint.work_id)
    authorization = {'dispatch_digest': progress._event_digest(dispatch),
                     'actor': 'owner', 'reason': 'Accept one additional call despite unknown cost'}
    resumed = authorize_model_repeat(queue, lease, checkpoint, request_digest='e' * 64,
                                     authorization=authorization)
    assert resumed.sequence == dispatch.sequence + 1
    assert resumed.details['artifact'] == checkpoint.details['artifact']
    assert authorize_model_repeat(queue, lease, checkpoint, request_digest='e' * 64,
                                  authorization=authorization) == resumed
    assert _run((queue, lease, resumed), _answer) == _answer()
    assert _run((queue, lease, resumed), _must_not_call) == _answer()


def test_repeat_authorization_cannot_discard_a_saved_answer(work):
    from compile_controller import authorize_model_repeat

    _run(work, _answer)
    queue, lease, checkpoint = work
    with pytest.raises(progress.ProgressConflict):
        authorize_model_repeat(queue, lease, checkpoint, request_digest='e' * 64,
                               authorization={'dispatch_digest': 'f' * 64,
                                              'actor': 'owner', 'reason': 'retry'})
    assert _run(work, _must_not_call) == _answer()


@pytest.fixture
def uncertain(work):
    def interrupted():
        raise SystemExit()

    with pytest.raises(SystemExit):
        _run(work, interrupted)
    queue, _, checkpoint = work
    dispatch = queue.get_compile_event(checkpoint.work_id)
    return {'dispatch_digest': progress._event_digest(dispatch), 'actor': 'owner', 'reason': 'retry'}


def _authorize(work, authorization):
    from compile_controller import authorize_model_repeat

    return authorize_model_repeat(*work, request_digest='e' * 64, authorization=authorization)


@pytest.mark.parametrize('field,value', [('dispatch_digest', 'f' * 64), ('actor', ''), ('reason', None)])
def test_invalid_repeat_authorization_leaves_dispatch_unchanged(work, uncertain, field, value):
    queue, _, checkpoint = work
    before = queue.get_compile_event(checkpoint.work_id)
    with pytest.raises(ValueError):
        _authorize(work, {**uncertain, field: value})
    assert queue.get_compile_event(checkpoint.work_id) == before


def test_repeat_authorization_does_not_allow_changed_request(work, uncertain):
    from compile_controller import run_model_call

    resumed = _authorize(work, uncertain)
    queue, lease, _ = work
    with pytest.raises(progress.ProgressConflict, match='another request'):
        run_model_call(queue, lease, resumed, request_digest='f' * 64, invoke=_must_not_call)
    assert _run((queue, lease, resumed), _answer) == _answer()


def test_authorization_recovers_an_orphan_answer_instead_of_repeating(work, monkeypatch):
    queue, _, checkpoint = work
    original = queue.record_compile_event

    def interrupt_answer(lease, event):
        if event.details.get('model_call', {}).get('phase') == 'answer':
            raise SystemExit()
        return original(lease, event)

    with monkeypatch.context() as patch:
        patch.setattr(queue, 'record_compile_event', interrupt_answer)
        with pytest.raises(SystemExit):
            _run(work, _answer)
    dispatch = queue.get_compile_event(checkpoint.work_id)
    authorization = {'dispatch_digest': progress._event_digest(dispatch), 'actor': 'owner', 'reason': 'retry'}
    with pytest.raises(progress.ProgressConflict, match='durable answer exists'):
        _authorize(work, authorization)
    assert _run(work, _must_not_call) == _answer()


def test_stale_lease_cannot_authorize_repeat(work, uncertain):
    queue, lease, _ = work
    queue.fail(lease, memory_queue.QueueFailure('unknown', blocked_capability='unknown'))
    with pytest.raises(memory_queue.LeaseFenceError):
        _authorize(work, uncertain)


def test_repeat_authorization_survives_redrive_without_erasing_history(work, uncertain):
    queue, lease, checkpoint = work
    queue.fail(lease, memory_queue.QueueFailure('worker_failed', permanent=True))
    queue.redrive(lease.id)
    child = queue.claim('redrive-worker')
    resumed = _authorize((queue, child, checkpoint), uncertain)
    assert _run((queue, child, resumed), _answer) == _answer()
    with queue._connect() as database:
        rows = database.execute(
            'SELECT task_id,sequence FROM compile_work_events WHERE work_id=? ORDER BY sequence',
            (checkpoint.work_id,),
        ).fetchall()
    assert [row['sequence'] for row in rows] == list(range(6))
    assert rows[2]['task_id'] == lease.id
    assert rows[3]['task_id'] == child.id


def test_repeat_authorization_commit_survives_lost_acknowledgement(work, uncertain, monkeypatch):
    queue, lease, checkpoint = work
    original = queue.record_compile_event

    def commit_then_stop(held, event):
        original(held, event)
        raise SystemExit()

    with monkeypatch.context() as patch:
        patch.setattr(queue, 'record_compile_event', commit_then_stop)
        with pytest.raises(SystemExit):
            _authorize(work, uncertain)
    reopened = memory_queue.active_memory_queue(queue.vault, queue.state_root)
    resumed = _authorize((reopened, lease, checkpoint), uncertain)
    assert resumed.sequence == checkpoint.sequence + 2
    assert _run((reopened, lease, resumed), _answer) == _answer()


def _blocked_unknown(work):
    from compile_controller import ProviderOutcomeUnknown

    def timeout():
        raise TimeoutError('possibly executed')

    with pytest.raises(ProviderOutcomeUnknown):
        _run(work, timeout)
    queue, lease, checkpoint = work
    return queue, lease, queue.get_compile_event(checkpoint.work_id)


def test_operator_claim_keeps_unknown_call_and_excludes_worker(work):
    queue, old, dispatch = _blocked_unknown(work)
    before = queue.get(old.id)
    lease = queue.claim_uncertain_compile(
        'operator', task_id=old.id, work_id=dispatch.work_id,
        dispatch_digest=progress._event_digest(dispatch),
    )
    assert lease.id == old.id
    assert lease.prior_attempts == before.attempts
    assert queue.claim('competing-worker', task_id=old.id) is None
    assert queue.get_compile_event(dispatch.work_id) == dispatch


def test_operator_claim_rejects_stale_dispatch_without_unblocking(work):
    queue, old, dispatch = _blocked_unknown(work)
    with pytest.raises(progress.ProgressConflict):
        queue.claim_uncertain_compile(
            'operator', task_id=old.id, work_id=dispatch.work_id,
            dispatch_digest='0' * 64,
        )
    assert queue.get(old.id).state == 'blocked'


def test_operator_claim_rolls_back_unblock_when_claim_fails(work, monkeypatch):
    queue, old, dispatch = _blocked_unknown(work)
    monkeypatch.setattr(queue, '_claim_in', lambda *args, **kwargs: None)
    with pytest.raises(memory_queue.QueueOperationError, match='compile_recovery_not_claimable'):
        queue.claim_uncertain_compile(
            'operator', task_id=old.id, work_id=dispatch.work_id,
            dispatch_digest=progress._event_digest(dispatch),
        )
    assert queue.get(old.id).state == 'blocked'
    assert queue.get(old.id).blocked_capability == 'provider_outcome_unknown'


def test_separate_unblock_claim_allows_worker_to_win(work):
    queue, old, _ = _blocked_unknown(work)
    queue.unblock(old.id)
    competitor = queue.claim('competing-worker', task_id=old.id)
    assert competitor.id == old.id
    assert queue.claim('operator', task_id=old.id) is None


def test_operator_claim_does_not_expose_ready_state(work, monkeypatch):
    queue, old, dispatch = _blocked_unknown(work)
    claim = queue._claim_in

    def inspect_before_claim(*args, **kwargs):
        # A separate connection sees the committed blocked state, never ready.
        assert queue.get(old.id).state == 'blocked'
        return claim(*args, **kwargs)

    monkeypatch.setattr(queue, '_claim_in', inspect_before_claim)
    lease = queue.claim_uncertain_compile(
        'operator', task_id=old.id, work_id=dispatch.work_id,
        dispatch_digest=progress._event_digest(dispatch),
    )
    assert queue.get(old.id).state == 'leased'
    assert lease.id == old.id


@pytest.mark.parametrize('state', ['ready', 'cancelled'])
def test_operator_claim_cannot_take_work_in_another_state(work, state):
    queue, old, dispatch = _blocked_unknown(work)
    {'ready': queue.unblock, 'cancelled': queue.cancel}[state](old.id)
    with pytest.raises(memory_queue.QueueOperationError, match='compile_recovery_requires_unknown_outcome'):
        queue.claim_uncertain_compile(
            'operator', task_id=old.id, work_id=dispatch.work_id,
            dispatch_digest=progress._event_digest(dispatch),
        )
    assert queue.get(old.id).state == state


def test_operator_claim_cannot_take_another_task_family(work):
    queue, old, dispatch = _blocked_unknown(work)
    other_id = queue.enqueue('compile', 1, {'daily': 'other'})
    other = queue.claim('worker', task_id=other_id)
    queue.fail(other, memory_queue.QueueFailure(
        'provider_outcome_unknown', blocked_capability='provider_outcome_unknown'))
    with pytest.raises(progress.ProgressConflict, match='another task family'):
        queue.claim_uncertain_compile(
            'operator', task_id=other_id, work_id=dispatch.work_id,
            dispatch_digest=progress._event_digest(dispatch),
        )
    assert queue.get(other_id).state == 'blocked'
    assert queue.get(old.id).state == 'blocked'


def _inspect_cli(queue, task_id, monkeypatch, capsys):
    import json
    import sys

    monkeypatch.setattr(memory_queue, 'active_memory_queue', lambda *args: queue)
    monkeypatch.setattr(sys, 'argv', ['memory_queue.py', 'compile-progress', task_id])
    code = memory_queue._cli()
    return code, json.loads(capsys.readouterr().out)


def test_operator_inspection_shows_dispatch_without_mutation(work, monkeypatch, capsys):
    queue, old, dispatch = _blocked_unknown(work)
    before = queue.db_path.read_bytes()
    code, result = _inspect_cli(queue, old.id, monkeypatch, capsys)
    assert code == 0
    assert result['state'] == 'blocked'
    assert result['answer_artifacts_checked'] is False
    assert result['history_audited'] is False
    assert result['works'][0]['event_digest'] == progress._event_digest(dispatch)
    assert result['works'][0]['model_call']['request_digest'] == 'e' * 64
    assert queue.db_path.read_bytes() == before
    assert queue.get_compile_event(dispatch.work_id) == dispatch


def test_operator_inspection_never_prints_source_or_answer(work, monkeypatch, capsys):
    import json

    _run(work, _answer)
    queue, lease, _ = work
    code, result = _inspect_cli(queue, lease.id, monkeypatch, capsys)
    assert code == 0
    assert result['works'][0]['model_call']['phase'] == 'answer'
    output = json.dumps(result)
    assert 'verified input snapshot' not in output
    assert 'queue-results' not in output
    assert 'artifact' not in result['works'][0]
    assert 'text' not in result['works'][0]['model_call']


def test_operator_inspection_includes_parent_work_after_redrive(work, monkeypatch, capsys):
    queue, lease, checkpoint = work
    _run(work, _answer)
    queue.fail(lease, memory_queue.QueueFailure('worker_failed', permanent=True))
    child = queue.redrive(lease.id)
    code, result = _inspect_cli(queue, child, monkeypatch, capsys)
    assert code == 0
    assert result['task_id'] == child
    assert [item['work_id'] for item in result['works']] == [checkpoint.work_id]
    assert result['works'][0]['task_id'] == lease.id


def test_operator_inspection_refuses_corrupt_checkpoint(work, monkeypatch, capsys):
    queue, old, dispatch = _blocked_unknown(work)
    with queue.connection() as database:
        database.execute('DROP TRIGGER compile_work_events_no_update')
        database.execute('UPDATE compile_work_events SET event_digest=? WHERE sequence=?',
                         ('0' * 64, dispatch.sequence))
        database.execute(progress.SCHEMA_STATEMENTS[1])
    code, result = _inspect_cli(queue, old.id, monkeypatch, capsys)
    assert code == 2
    assert result == {'codes': ['compile_progress_corrupt']}


def test_operator_inspection_refuses_corrupt_payload_without_quarantining(work, monkeypatch, capsys):
    queue, lease, _ = work
    with queue.connection() as database:
        database.execute('UPDATE tasks SET payload_blob=? WHERE id=?',
                         (b'{"private":"never print this"}', lease.id))
    before = queue.db_path.read_bytes()
    code, result = _inspect_cli(queue, lease.id, monkeypatch, capsys)
    assert code == 2
    assert result == {'codes': ['redrive_lineage_payload_mismatch']}
    assert queue.db_path.read_bytes() == before


@pytest.mark.parametrize('field,value', [('request_digest', 'private prompt'), ('phase', 'private text')])
def test_operator_inspection_does_not_export_malformed_call_fields(work, monkeypatch, capsys, field, value):
    import json

    queue, old, dispatch = _blocked_unknown(work)
    bad = replace(dispatch, details={'model_call': {**dispatch.details['model_call'], field: value}})
    with queue.connection() as database:
        database.execute('DROP TRIGGER compile_work_events_no_update')
        database.execute(
            'UPDATE compile_work_events SET details_json=?,event_digest=? WHERE work_id=? AND sequence=?',
            (json.dumps(bad.details), progress._event_digest(bad), bad.work_id, bad.sequence))
        database.execute(progress.SCHEMA_STATEMENTS[1])
    code, result = _inspect_cli(queue, old.id, monkeypatch, capsys)
    assert code == 2
    assert result == {'codes': ['compile_progress_corrupt']}


def test_operator_inspection_does_not_recover_orphan_answer(work, monkeypatch, capsys):
    queue, lease, checkpoint = work
    original = queue.record_compile_event

    def interrupt_answer(owned, event):
        if event.details.get('model_call', {}).get('phase') == 'answer':
            raise SystemExit()
        return original(owned, event)

    with monkeypatch.context() as patch:
        patch.setattr(queue, 'record_compile_event', interrupt_answer)
        with pytest.raises(SystemExit):
            _run(work, _answer)
    dispatch = queue.get_compile_event(checkpoint.work_id)
    code, result = _inspect_cli(queue, lease.id, monkeypatch, capsys)
    assert code == 0
    assert result['answer_artifacts_checked'] is False
    assert result['works'][0]['model_call']['phase'] == 'dispatch'
    assert queue.get_compile_event(checkpoint.work_id) == dispatch
    assert _run(work, _must_not_call) == _answer()


def test_operator_inspection_real_cli_uses_adopted_journal(work, upgraded):
    import json
    import os
    import subprocess
    import sys

    queue, lease, dispatch = _blocked_unknown(work)
    root, state, _ = upgraded
    environment = {**os.environ, 'LLM_WIKI_ROOT': str(root), 'LLM_WIKI_STATE_ROOT': str(state)}
    before = queue.db_path.read_bytes()
    result = subprocess.run(
        [sys.executable, memory_queue.__file__, 'compile-progress', lease.id],
        env=environment, capture_output=True, text=True, check=True,
    )
    report = json.loads(result.stdout)
    assert report['works'][0]['event_digest'] == progress._event_digest(dispatch)
    assert queue.db_path.read_bytes() == before


def test_recorded_repeat_decision_is_idempotent_after_redrive(work, uncertain):
    queue, lease, checkpoint = work
    resumed = _authorize(work, uncertain)
    queue.fail(lease, memory_queue.QueueFailure('operator_stopped', permanent=True))
    child_id = queue.redrive(lease.id)
    child = queue.claim('recovery', task_id=child_id)
    assert _authorize((queue, child, checkpoint), uncertain) == resumed
    assert queue.get_compile_event(checkpoint.work_id) == resumed
    assert resumed.task_id == lease.id


def _resolve(work, **options):
    from compile_controller import resolve_model_call

    queue, lease, checkpoint = work
    dispatch = queue.get_compile_event(checkpoint.work_id, sequence=checkpoint.sequence + 1)
    return resolve_model_call(queue, lease, dispatch, **options)


def test_resolution_without_consent_preserves_unknown_call(work, uncertain):
    queue, lease, _ = work
    before = queue.db_path.read_bytes()
    outcome, dispatch = _resolve(work)
    assert outcome == 'outcome_unknown'
    assert progress._event_digest(dispatch) == uncertain['dispatch_digest']
    assert queue.db_path.read_bytes() == before
    assert queue.get(lease.id).state == 'leased'


def test_resolution_reuses_saved_answer_without_exposing_response(work):
    queue, _, checkpoint = work
    _run(work, _answer)
    outcome, event = _resolve(work)
    assert outcome == 'answer_recovered'
    assert event == queue.get_compile_event(checkpoint.work_id)
    assert event.details['model_call']['phase'] == 'answer'
    assert _resolve(work) == (outcome, event)


def test_resolution_recovers_orphan_answer_even_with_repeat_consent(work, monkeypatch):
    queue, _, checkpoint = work
    original = queue.record_compile_event

    def interrupt_answer(lease, event):
        if event.details.get('model_call', {}).get('phase') == 'answer':
            raise SystemExit()
        return original(lease, event)

    with monkeypatch.context() as patch:
        patch.setattr(queue, 'record_compile_event', interrupt_answer)
        with pytest.raises(SystemExit):
            _run(work, _answer)
    dispatch = queue.get_compile_event(checkpoint.work_id)
    authorization = {'dispatch_digest': progress._event_digest(dispatch), 'actor': 'owner', 'reason': 'retry'}
    outcome, event = _resolve(work, authorization=authorization)
    assert outcome == 'answer_recovered'
    assert 'repeat_authorization' not in event.details
    assert _run(work, _must_not_call) == _answer()


def test_resolution_reuses_committed_consent_after_redrive_without_new_consent(work, uncertain):
    queue, lease, checkpoint = work
    outcome, event = _resolve(work, authorization=uncertain)
    assert outcome == 'repeat_authorized'
    queue.fail(lease, memory_queue.QueueFailure('operator_stopped', permanent=True))
    child_id = queue.redrive(lease.id)
    child = queue.claim('recovery', task_id=child_id)
    assert _resolve((queue, child, checkpoint)) == (outcome, event)
    assert event.task_id == lease.id
    assert _run((queue, child, event), _answer) == _answer()


def test_resolution_rejects_changed_consent_after_commit(work, uncertain):
    queue, _, checkpoint = work
    _, event = _resolve(work, authorization=uncertain)
    with pytest.raises(progress.ProgressConflict):
        _resolve(work, authorization={**uncertain, 'reason': 'different decision'})
    assert queue.get_compile_event(checkpoint.work_id) == event


def test_resolution_rejects_forged_dispatch(work, uncertain):
    from compile_controller import resolve_model_call

    queue, lease, checkpoint = work
    dispatch = queue.get_compile_event(checkpoint.work_id)
    with pytest.raises(progress.ProgressConflict, match='retained dispatch'):
        resolve_model_call(queue, lease, replace(dispatch, source_digest='f' * 64))
    assert queue.get_compile_event(checkpoint.work_id) == dispatch


def test_resolution_requires_current_lease_even_for_saved_answer(work):
    _run(work, _answer)
    queue, lease, checkpoint = work
    with pytest.raises(memory_queue.LeaseFenceError):
        _resolve((queue, replace(lease, token='stale'), checkpoint))


def test_resolution_cannot_override_corrupt_answer_with_consent(work):
    queue, _, checkpoint = work
    _run(work, _answer)
    answer = queue.get_compile_event(checkpoint.work_id)
    dispatch = queue.get_compile_event(checkpoint.work_id, sequence=checkpoint.sequence + 1)
    path = queue.state_root / answer.details['artifact']['reference']
    path.write_bytes(b'corrupt saved answer')
    authorization = {'dispatch_digest': progress._event_digest(dispatch), 'actor': 'owner', 'reason': 'retry'}
    with pytest.raises(progress.ProgressCorrupt):
        _resolve(work, authorization=authorization)
    assert queue.get_compile_event(checkpoint.work_id) == answer


def test_resolution_consent_survives_lost_ack_and_redrive(work, uncertain, monkeypatch):
    queue, lease, checkpoint = work
    original = queue.record_compile_event

    def commit_then_stop(owned, event):
        original(owned, event)
        raise SystemExit()

    with monkeypatch.context() as patch:
        patch.setattr(queue, 'record_compile_event', commit_then_stop)
        with pytest.raises(SystemExit):
            _resolve(work, authorization=uncertain)
    recorded = queue.get_compile_event(checkpoint.work_id)
    queue.fail(lease, memory_queue.QueueFailure('operator_stopped', permanent=True))
    child_id = queue.redrive(lease.id)
    child = queue.claim('recovery', task_id=child_id)
    assert _resolve((queue, child, checkpoint)) == ('repeat_authorized', recorded)


def test_resolution_uses_atomic_unknown_dispatch_claim(work):
    from compile_controller import resolve_model_call

    queue, old, dispatch = _blocked_unknown(work)
    lease = queue.claim_uncertain_compile(
        'operator', task_id=old.id, work_id=dispatch.work_id,
        dispatch_digest=progress._event_digest(dispatch),
    )
    assert resolve_model_call(queue, lease, dispatch) == ('outcome_unknown', dispatch)
    assert queue.get(old.id).state == 'leased'


def test_resolution_cannot_read_another_task_family(work, uncertain):
    from compile_controller import resolve_model_call

    queue, _, checkpoint = work
    other_id = queue.enqueue('compile', 1, {'daily': 'unrelated recovery'})
    other = queue.claim('other-worker', task_id=other_id)
    dispatch = queue.get_compile_event(checkpoint.work_id)
    with pytest.raises(progress.ProgressConflict, match='another task family'):
        resolve_model_call(queue, other, dispatch, authorization=uncertain)
    assert queue.get_compile_event(checkpoint.work_id) == dispatch


@pytest.mark.parametrize('operation', ['repeat', 'resolve'])
def test_recorded_consent_cannot_be_rebound_to_unrelated_delivery(work, uncertain, operation):
    queue, _, _ = work
    recorded = _authorize(work, uncertain)
    other_id = queue.enqueue('compile', 1, {'daily': 'unrelated decision'})
    forged = replace(recorded, task_id=other_id)
    with queue.connection() as database:
        database.execute('DROP TRIGGER compile_work_events_no_update')
        database.execute(
            'UPDATE compile_work_events SET task_id=?,event_digest=? WHERE work_id=? AND sequence=?',
            (other_id, progress._event_digest(forged), recorded.work_id, recorded.sequence))
        database.execute(progress.SCHEMA_STATEMENTS[1])
    actions = {'repeat': lambda: _authorize(work, uncertain), 'resolve': lambda: _resolve(work)}
    with pytest.raises(progress.ProgressConflict, match='another task family'):
        actions[operation]()
