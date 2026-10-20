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
