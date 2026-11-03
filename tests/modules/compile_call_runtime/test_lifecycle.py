"""Pin value evidence and observable ordering at the call lifecycle boundary."""
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from context_budget import TokenUsage
from llm_wiki import compile_call_runtime as runtime
from llm_wiki import compile_event_records as progress
from llm_wiki.model_records import BackendResponse
from llm_wiki.queue_contracts import QueueLease

ORIGINAL = json.loads((Path(__file__).parent / 'fixtures/checkpoints.json').read_text(encoding='utf-8'))


def _event(name):
    return progress.CompileEvent(**ORIGINAL[name])


def _lease():
    now = datetime(2000, 1, 1, tzinfo=UTC)
    return QueueLease('delivery-task', 'compile', 1, {}, 'a' * 64, 'worker', 'token',
                      now, 1, now, now, 0)


def test_dispatch_and_repeat_keep_original_serialized_values():
    checkpoint = _event('checkpoint')
    dispatch = runtime._dispatch_event(checkpoint, 'delivery-task', ORIGINAL['request_digest'])
    assert dispatch == _event('dispatch')
    repeat = runtime._repeat_checkpoint(checkpoint, 'delivery-task', ORIGINAL['request_digest'],
                                        ORIGINAL['authorization'])
    assert repeat == _event('repeat')
    runtime._require_resolution_consent(dispatch, repeat.details['repeat_authorization'])


@pytest.mark.parametrize('changes, error', [
    ({'stage': 'pending'}, progress.ProgressConflict),
    ({'outcome': 'running'}, progress.ProgressConflict),
    ({'details': {}}, progress.ProgressCorrupt),
    ({'details': _event('dispatch').details}, progress.ProgressConflict),
])
def test_nonready_or_unbound_checkpoint_is_refused(changes, error):
    with pytest.raises(error):
        runtime._require_checkpoint(replace(_event('checkpoint'), **changes))


@pytest.mark.parametrize('changes', [
    {'actor': ''}, {'actor': None}, {'reason': ' '}, {'reason': False},
    {'dispatch_digest': 'invalid'}, {'extra': 'not consent'},
])
def test_invalid_repeat_authorization_is_refused(changes):
    authorization = {**ORIGINAL['authorization'], **changes}
    with pytest.raises(ValueError):
        runtime._repeat_checkpoint(_event('checkpoint'), 'delivery-task',
                                   ORIGINAL['request_digest'], authorization)


def test_repeat_is_bound_to_request_and_dispatch():
    with pytest.raises(progress.ProgressConflict):
        runtime._dispatch_event(_event('repeat'), 'delivery-task', '0' * 64)
    authorization = {**ORIGINAL['authorization'], 'dispatch_digest': '0' * 64}
    with pytest.raises(progress.ProgressConflict):
        runtime._require_resolution_consent(_event('dispatch'), authorization)


def test_delivery_cannot_be_reentered_or_reused_after_exit():
    queue, lease = Mock(), _lease()
    scope = runtime.CompileDelivery(queue, lease)
    with pytest.raises(progress.ProgressConflict, match='not active'):
        scope.require_owner(queue, lease)
    with scope:
        scope.require_owner(queue, replace(lease, expires_at=lease.expires_at + timedelta(seconds=1)))
        with pytest.raises(progress.ProgressConflict, match='already entered'):
            scope.__enter__()
    with pytest.raises(progress.ProgressConflict, match='not active'):
        scope.require_owner(queue, lease)
    queue.fail.assert_not_called()


@pytest.mark.parametrize('changes', [
    {'owner': 'other'}, {'token': 'other'}, {'id': 'other'}, {'payload': {'other': True}},
])
def test_delivery_does_not_accept_changed_lease_identity(changes):
    queue, lease = Mock(), _lease()
    with runtime.CompileDelivery(queue, lease) as scope:
        with pytest.raises(progress.ProgressConflict, match='another owner'):
            scope.require_owner(queue, replace(lease, **changes))
        with pytest.raises(progress.ProgressConflict, match='another owner'):
            scope.require_owner(Mock(), lease)


def _ready_queue():
    queue = Mock()
    queue.get_compile_event.return_value = _event('checkpoint')
    queue.transport.return_value = BackendResponse('answer', TokenUsage(input_tokens=0, output_tokens=1))
    return queue


def test_reservation_precedes_transport_and_answer_storage():
    queue = _ready_queue()
    result = runtime.run_model_call(queue, _lease(), _event('checkpoint'),
                                    request_digest=ORIGINAL['request_digest'], invoke=queue.transport)
    assert result == queue.transport.return_value
    assert [call[0] for call in queue.mock_calls] == [
        'verify_compile_checkpoint_owner', 'get_compile_event', 'verify_compile_artifact',
        'record_compile_event', 'transport', 'save_compile_artifact',
    ]
    assert queue.record_compile_event.call_args.args == (_lease(), _event('dispatch'))
    queue.fail.assert_not_called()


def test_catching_unknown_outcome_cannot_turn_delivery_into_success():
    queue = _ready_queue()
    queue.transport.side_effect = TimeoutError('uncertain transport')
    with pytest.raises(runtime.ProviderOutcomeUnknown, match='delivery retains'):
        with runtime.CompileDelivery(queue, _lease()) as scope:
            with pytest.raises(runtime.ProviderOutcomeUnknown):
                scope.run(_event('checkpoint'), request_digest=ORIGINAL['request_digest'],
                          invoke=queue.transport)
            queue.fail.assert_not_called()
    queue.fail.assert_called_once()
    failure = queue.fail.call_args.args[1]
    assert failure.error_code == failure.blocked_capability == 'provider_outcome_unknown'
    queue.save_compile_artifact.assert_not_called()


def test_source_conflict_never_reaches_transport():
    queue = _ready_queue()
    queue.record_compile_event.side_effect = progress.SourceOutcomeConflict(_event('dispatch'))
    with pytest.raises(runtime.ProviderOutcomeUnknown, match='source_outcome_unknown'):
        runtime.run_model_call(queue, _lease(), _event('checkpoint'),
                               request_digest=ORIGINAL['request_digest'], invoke=queue.transport)
    queue.transport.assert_not_called()
    queue.fail.assert_called_once()
