"""Durable external-call boundary for the approved compile controller.

Not yet activated by CLI/MCP/nightly. The shared client prepares protected calls;
semantic callers still own complete work/dependency identity and publication.
"""
from __future__ import annotations

import json
from dataclasses import asdict, replace
from functools import partial

import compile_progress as progress
from context_budget import TokenCount, TokenUsage
from llm_client import BackendResponse, LLMResult, ProviderDescriptor
from reliable_memory import canonical_json_bytes

_COMPLETED_REFUSALS = frozenset({'empty_response', 'dlp_output_blocked', 'dlp_scan_error'})


class ProviderOutcomeUnknown(RuntimeError):
    """An external call may have run; automatic redispatch is forbidden."""


def _require_checkpoint(checkpoint):
    allowed = {'snapshot_ready', 'draft_ready', 'review_ready'}
    if checkpoint.stage not in allowed or checkpoint.outcome != 'ready':
        raise progress.ProgressConflict('model call requires a ready semantic checkpoint')
    if 'model_call' in checkpoint.details:
        raise progress.ProgressConflict('an answer requires semantic validation before another call')
    progress.artifact_identity(checkpoint)


def _dispatch_event(checkpoint, task_id, request_digest):
    progress._require_digest(request_digest)
    _require_repeat_request(checkpoint, request_digest)
    identity = {
        'schema': 'compile-model-call/v1', 'request_digest': request_digest,
        'checkpoint_sequence': checkpoint.sequence,
        'checkpoint_digest': progress._event_digest(checkpoint), 'phase': 'dispatch',
    }
    return replace(checkpoint, task_id=task_id, sequence=checkpoint.sequence + 1,
                   outcome='running', details={'model_call': identity})


def _answer_event(dispatch, task_id):
    call = {**dispatch.details['model_call'], 'phase': 'answer'}
    return replace(dispatch, task_id=task_id, sequence=dispatch.sequence + 1,
                   outcome='ready', details={'model_call': call})


def _call_binding(dispatch):
    return {
        'work_id': dispatch.work_id, 'source_digest': dispatch.source_digest,
        'stage': dispatch.stage, 'sequence': dispatch.sequence,
        'model_call': dispatch.details['model_call'],
    }


def _encoded_answer(dispatch, response):
    if isinstance(response, LLMResult):
        return _encoded_candidate_result(dispatch, response)
    if not isinstance(response, BackendResponse):
        raise TypeError('protected transport must return BackendResponse')
    _require_response(response)
    return canonical_json_bytes({
        'schema': 'compile-model-answer/v1', 'dispatch': _call_binding(dispatch),
        'text': response.text, 'usage': asdict(response.usage),
    })


def _require_response(response):
    if not isinstance(response.text, str):
        raise ValueError('model answer text must be a string')
    # Reuse the shared usage contract; unknown values remain None, not zero.
    TokenUsage(**asdict(response.usage))


def _decoded_answer(dispatch, data):
    try:
        value = json.loads(data)
        if canonical_json_bytes(value) != data:
            raise ValueError('model answer encoding is not canonical')
        return _decoded_response(dispatch, value)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise progress.ProgressCorrupt('saved model answer is invalid or belongs to another call') from error


def _require_answer_binding(dispatch, value):
    expected = {'schema', 'dispatch', 'text', 'usage'}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError('model answer envelope changed')
    if (value['schema'], value['dispatch']) != ('compile-model-answer/v1', _call_binding(dispatch)):
        raise ValueError('model answer identity changed')


def _require_same_call(current, dispatch):
    if current is None:
        raise progress.ProgressConflict('compile checkpoint is not recorded')
    expected = _answer_event(dispatch, dispatch.task_id)
    # Task ownership may change through redelivery; sequence and call identity may not.
    details = dict(current.details)
    details.pop('artifact', None)
    identity = replace(current, task_id=dispatch.task_id, details=details)
    if identity not in (dispatch, expected):
        raise progress.ProgressConflict('compile call identity or progress changed')


def _resume_call(queue, lease, dispatch, current):
    _require_same_call(current, dispatch)
    if current.sequence == dispatch.sequence + 1:
        return _decoded_answer(dispatch, queue.read_compile_artifact(current))
    answer = _answer_event(dispatch, lease.id)
    recovered = queue.recover_compile_artifact(
        lease, answer, validate=partial(_decoded_answer, dispatch),
    )
    if recovered is None:
        raise ProviderOutcomeUnknown('provider_outcome_unknown: no durable answer; repeat requires authorization')
    return _decoded_answer(dispatch, queue.read_compile_artifact(recovered))


def _invoke_and_save(queue, lease, dispatch, invoke):
    try:
        response = invoke()
        data = _encoded_answer(dispatch, response)
    except Exception as error:
        raise ProviderOutcomeUnknown('provider_outcome_unknown: transport did not return a durable answer') from error
    queue.save_compile_artifact(lease, _answer_event(dispatch, lease.id), data)
    return response


def run_model_call(queue, lease, checkpoint, *, request_digest, invoke):
    """Reserve once, or recover an answer; never silently repeat uncertain cost.

    `invoke` must use the existing DLP-protected transport. A successful response
    is still only an answer, not a validated plan or a publication receipt.
    """
    checkpoint = progress._snapshot_event(checkpoint)
    _require_checkpoint(checkpoint)
    queue.verify_compile_checkpoint_owner(lease, checkpoint)
    dispatch = _dispatch_event(checkpoint, lease.id, request_digest)
    try:
        return _run_reserved_call(queue, lease, checkpoint, dispatch, invoke)
    except ProviderOutcomeUnknown:
        _block_unknown_call(queue, lease)
        raise


def _block_unknown_call(queue, lease):
    from memory_queue import QueueFailure

    queue.fail(lease, QueueFailure('provider_outcome_unknown',
                                  blocked_capability='provider_outcome_unknown'))


def _run_reserved_call(queue, lease, checkpoint, dispatch, invoke):
    current = queue.get_compile_event(checkpoint.work_id)
    if current != checkpoint:
        return _resume_recorded_call(queue, lease, dispatch, current)
    queue.read_compile_artifact(checkpoint)
    queue.record_compile_event(lease, dispatch)
    return _invoke_and_save(queue, lease, dispatch, invoke)


def _repeat_checkpoint(checkpoint, task_id, request_digest, authorization):
    if set(authorization) != {'dispatch_digest', 'actor', 'reason'}:
        raise ValueError('repeat authorization requires dispatch identity, actor and reason')
    progress._require_digest(authorization['dispatch_digest'])
    _require_authorization_text(authorization)
    details = {**checkpoint.details, 'repeat_authorization': {
        **authorization, 'request_digest': request_digest,
    }}
    return progress._snapshot_event(replace(
        checkpoint, task_id=task_id, sequence=checkpoint.sequence + 2, details=details,
    ))


def _require_authorization_text(authorization):
    for field in ('actor', 'reason'):
        value = authorization[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError('repeat authorization requires a nonempty actor and reason')


def _require_repeat_request(checkpoint, request_digest):
    authorization = checkpoint.details.get('repeat_authorization')
    if authorization is not None and authorization['request_digest'] != request_digest:
        raise progress.ProgressConflict('repeat authorization belongs to another request')


def authorize_model_repeat(queue, lease, checkpoint, *, request_digest, authorization):
    """Record an explicit operator decision for one identified uncertain call.

    Internal API, not an authorization verifier: the eventual operator entrypoint
    must establish consent and supply its actor/reason. Unblocking or redriving a
    task must never call this automatically. Retained history and unknown costs
    are not reset. This function neither invokes a provider nor publishes memory.
    """
    checkpoint = progress._snapshot_event(checkpoint)
    _require_checkpoint(checkpoint)
    queue.verify_compile_checkpoint_owner(lease, checkpoint)
    dispatch = _dispatch_event(checkpoint, lease.id, request_digest)
    resumed = _repeat_checkpoint(checkpoint, lease.id, request_digest, authorization)
    current = queue.get_compile_event(checkpoint.work_id)
    if current == resumed:
        queue.read_compile_artifact(current)
        return current
    return _authorize_uncertain_call(queue, lease, dispatch, current, resumed)


def _authorize_uncertain_call(queue, lease, dispatch, current, resumed):
    _require_same_call(current, dispatch)
    if progress._event_digest(current) != resumed.details['repeat_authorization']['dispatch_digest']:
        raise progress.ProgressConflict('repeat authorization names another dispatch')
    try:
        _resume_call(queue, lease, dispatch, current)
    except ProviderOutcomeUnknown:
        queue.record_compile_event(lease, resumed)
        return resumed
    raise progress.ProgressConflict('a durable answer exists; resume it instead of repeating')


def _decoded_response(dispatch, value):
    if value.get('schema') == 'compile-candidate-result/v1':
        return _decoded_candidate_result(dispatch, value)
    _require_answer_binding(dispatch, value)
    response = BackendResponse(value['text'], TokenUsage(**value['usage']))
    _require_response(response)
    return response


def _require_completed_result(result):
    if result.available is not True or result.structured_output not in {'native', 'prompt'}:
        raise ValueError('a dispatched result must describe an available prepared provider')
    if result.text is not None:
        _require_result_text(result)
        return
    _require_completed_refusal(result)


def _require_result_text(result):
    if not isinstance(result.text, str) or result.failure_class is not None:
        raise ValueError('completed answer has invalid text or a failure')


def _require_completed_refusal(result):
    # These outcomes follow an actual returned response and the shared output scan.
    if result.failure_class not in _COMPLETED_REFUSALS:
        raise ValueError('provider outcome is uncertain; a returned error is not a saved answer')


def _result_document(result):
    _require_completed_result(result)
    TokenUsage(**asdict(result.usage))
    return {
        'descriptor': result.descriptor.canonical(), 'text': result.text,
        'available': result.available, 'failure_class': result.failure_class,
        'structured_output': result.structured_output, 'usage': asdict(result.usage),
        'input_token_count': _count_document(result.input_token_count),
        'failure_detail': result.failure_detail,
    }


def _count_document(count):
    if count is None:
        return None
    TokenCount(**asdict(count))
    return asdict(count)


def _encoded_candidate_result(dispatch, result):
    return canonical_json_bytes({
        'schema': 'compile-candidate-result/v1', 'dispatch': _call_binding(dispatch),
        'result': _result_document(result),
    })


def _decoded_candidate_result(dispatch, value):
    if set(value) != {'schema', 'dispatch', 'result'} or value['dispatch'] != _call_binding(dispatch):
        raise ValueError('saved candidate result belongs to another call')
    result = _result_from_document(value['result'])
    if _result_document(result) != value['result']:
        raise ValueError('saved candidate result is not lossless')
    return result


def _result_from_document(document):
    from types import MappingProxyType

    fields = dict(document)
    descriptor = dict(fields.pop('descriptor'))
    descriptor['fallback_from'] = tuple(descriptor['fallback_from'])
    descriptor['capabilities'] = MappingProxyType(descriptor['capabilities'])
    descriptor['inference_settings'] = MappingProxyType(descriptor['inference_settings'])
    fields['usage'] = TokenUsage(**fields['usage'])
    fields['input_token_count'] = _count_from_document(fields['input_token_count'])
    return LLMResult(ProviderDescriptor(**descriptor), **fields)


def _count_from_document(value):
    if value is None:
        return None
    return TokenCount(**value)


def run_candidate_call(queue, lease, checkpoint, *, dependency_digest, descriptor,
                       prompt, system_prompt, **options):
    """Use the shared DLP/preparation path, journaling only external invocation.

    The semantic caller still owns the complete work/dependency identity. The
    protected-request hash alone does not bind source snapshots or compiler code.
    """
    from llm_client import call_candidate

    progress._require_digest(dependency_digest)
    dispatch = partial(_journal_candidate, queue, lease, checkpoint,
                       dependency_digest, descriptor)
    return call_candidate(descriptor, prompt, system_prompt, dispatch=dispatch, **options)


def _journal_candidate(queue, lease, checkpoint, dependency_digest, descriptor, prepared):
    from reliable_memory import sha256_bytes

    request = sha256_bytes(canonical_json_bytes({
        'protected_request': prepared.request_digest, 'dependencies': dependency_digest,
    }))
    result = run_model_call(queue, lease, checkpoint, request_digest=request,
                           invoke=partial(_invoke_candidate, prepared, descriptor))
    _require_candidate_identity(result, descriptor)
    return result


def _invoke_candidate(prepared, descriptor):
    result = prepared.invoke()
    _require_candidate_identity(result, descriptor)
    return result


def _require_candidate_identity(result, descriptor):
    if not isinstance(result, LLMResult) or result.descriptor.canonical() != descriptor.canonical():
        raise ValueError('candidate result identity differs from prepared request')


def _resume_recorded_call(queue, lease, dispatch, current):
    if current is not None and current.sequence > dispatch.sequence + 1:
        return _resume_historical_call(queue, lease, dispatch)
    return _resume_call(queue, lease, dispatch, current)


def _resume_historical_call(queue, lease, dispatch):
    recorded = queue.get_compile_event(dispatch.work_id, sequence=dispatch.sequence)
    if recorded is None or replace(recorded, task_id=dispatch.task_id) != dispatch:
        raise progress.ProgressConflict('historical compile dispatch identity changed')
    queue.verify_compile_checkpoint_owner(lease, recorded)
    answer = queue.get_compile_event(dispatch.work_id, sequence=dispatch.sequence + 1)
    _require_same_call(answer, dispatch)
    queue.verify_compile_checkpoint_owner(lease, answer)
    return _decoded_answer(dispatch, queue.read_compile_artifact(answer))


class CompileReplay:
    """Replay checked call results while existing semantic validators run again.

    This cursor neither certifies publication nor invents work/dependency identity.
    Its caller supplies a verified snapshot checkpoint and the dependency digest.
    """

    def __init__(self, queue, lease, checkpoint, *, dependency_digest):
        progress._require_digest(dependency_digest)
        _require_checkpoint(checkpoint)
        self.queue = queue
        self.lease = lease
        self.checkpoint = progress._snapshot_event(checkpoint)
        self.dependency_digest = dependency_digest

    def __call__(self, descriptor, prompt, system_prompt, **options):
        result = run_candidate_call(
            self.queue, self.lease, self.checkpoint, dependency_digest=self.dependency_digest,
            descriptor=descriptor, prompt=prompt, system_prompt=system_prompt, **options,
        )
        if _is_completed_result(result):
            self.checkpoint = _advance_replay(self.queue, self.lease, self.checkpoint)
        return result


def _advance_replay(queue, lease, checkpoint):
    answer = queue.get_compile_event(checkpoint.work_id, sequence=checkpoint.sequence + 2)
    if answer is None:
        return checkpoint  # A preflight refusal made no dispatch reservation.
    if answer.details.get('model_call', {}).get('phase') != 'answer':
        raise progress.ProgressConflict('replay requires a completed recorded call')
    queue.verify_compile_checkpoint_owner(lease, answer)
    queue.read_compile_artifact(answer)
    following = replace(checkpoint, task_id=lease.id, sequence=answer.sequence + 1,
                        details={'artifact': checkpoint.details['artifact'],
                                 'replay': {'answer_digest': progress._event_digest(answer)}})
    return _record_or_read_replay_checkpoint(queue, lease, following)


def _record_or_read_replay_checkpoint(queue, lease, following):
    recorded = queue.get_compile_event(following.work_id, sequence=following.sequence)
    if recorded is None:
        queue.record_compile_event(lease, following)
        return following
    queue.verify_compile_checkpoint_owner(lease, recorded)
    if replace(recorded, task_id=lease.id) != following:
        raise progress.ProgressConflict('semantic replay checkpoint identity changed')
    return recorded


def _is_completed_result(result):
    return result.available is True and (
        result.text is not None or result.failure_class in _COMPLETED_REFUSALS
    )
