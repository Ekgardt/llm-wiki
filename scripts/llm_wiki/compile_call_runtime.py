"""Existing durable model-call lifecycle through explicit queue and transport inputs."""
from __future__ import annotations

from dataclasses import replace
from functools import partial

import llm_wiki.compile_event_records as progress
from llm_wiki.compile_answer_records import (
    _answer_event,
    _decoded_answer,
    _encoded_answer,
    _require_same_call,
)


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


class CompileDelivery:
    """Keep a delivery lease through independent work; finalize uncertainty once.

    Calls still reserve immutable work identities before transport. Catching an
    unknown outcome inside this scope cannot authorize its repeat or turn the
    delivery into success. Publication/normal task completion remain the caller's
    responsibility, as does renewing the existing queue lease.
    """

    def __init__(self, queue, lease):
        self.queue = queue
        self.lease = lease
        self._entered = False
        self._active = False
        self._unknown = False

    def __enter__(self):
        if self._entered:
            raise progress.ProgressConflict('compile delivery already entered')
        self._entered = True
        self._active = True
        return self

    def __exit__(self, exception_type, exception, traceback):
        self._active = False
        if not self._unknown:
            return False
        _block_unknown_call(self.queue, self.lease)
        if exception_type is None:
            raise ProviderOutcomeUnknown('provider_outcome_unknown: delivery retains an uncertain call')
        return False

    def require_owner(self, queue, lease):
        self._require_active()
        same_lease = replace(lease, expires_at=self.lease.expires_at) == self.lease
        if queue is not self.queue or not same_lease:
            raise progress.ProgressConflict('compile delivery belongs to another owner')

    def _require_active(self):
        if not self._active:
            raise progress.ProgressConflict('compile delivery is not active')

    def run(self, checkpoint, *, request_digest, invoke):
        self._require_active()
        checkpoint = progress._snapshot_event(checkpoint)
        _require_checkpoint(checkpoint)
        self.queue.verify_compile_checkpoint_owner(self.lease, checkpoint)
        dispatch = _dispatch_event(checkpoint, self.lease.id, request_digest)
        try:
            return _run_reserved_call(self.queue, self.lease, checkpoint, dispatch, invoke)
        except ProviderOutcomeUnknown:
            self._unknown = True
            raise


def run_model_call(queue, lease, checkpoint, *, request_digest, invoke, delivery=None):
    """Reserve/recover within a delivery, never silently repeat uncertain cost.

    `invoke` must use the existing DLP-protected transport. Without an explicit
    active scope this call is its own delivery and blocks the queue immediately
    on uncertainty, preserving the single-call contract.
    """
    if delivery is not None:
        delivery.require_owner(queue, lease)
        return delivery.run(checkpoint, request_digest=request_digest, invoke=invoke)
    with CompileDelivery(queue, lease) as owned:
        return owned.run(checkpoint, request_digest=request_digest, invoke=invoke)


def _block_unknown_call(queue, lease):
    from llm_wiki.queue_contracts import QueueFailure

    queue.fail(lease, QueueFailure('provider_outcome_unknown',
                                  blocked_capability='provider_outcome_unknown'))


def _run_reserved_call(queue, lease, checkpoint, dispatch, invoke):
    current = queue.get_compile_event(checkpoint.work_id)
    if current != checkpoint:
        return _resume_recorded_call(queue, lease, dispatch, current)
    queue.verify_compile_artifact(checkpoint)
    try:
        queue.record_compile_event(lease, dispatch)
    except progress.SourceOutcomeConflict as error:
        raise ProviderOutcomeUnknown(f'source_outcome_unknown: {error}') from error
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
    recorded = _recorded_repeat(queue, lease, current, resumed)
    if recorded is not None:
        return recorded
    return _authorize_uncertain_call(queue, lease, dispatch, current, resumed)


def _recorded_repeat(queue, lease, current, resumed):
    if current is None or resumed is None:
        return None
    if replace(current, task_id=resumed.task_id) != resumed:
        return None
    queue.verify_compile_checkpoint_owner(lease, current)
    queue.verify_compile_artifact(current)
    return current


def _authorize_uncertain_call(queue, lease, dispatch, current, resumed):
    _require_same_call(current, dispatch)
    if progress._event_digest(current) != resumed.details['repeat_authorization']['dispatch_digest']:
        raise progress.ProgressConflict('repeat authorization names another dispatch')
    outcome, event = _resolve_or_authorize(queue, lease, dispatch, current, resumed)
    if outcome == 'repeat_authorized':
        return event
    raise progress.ProgressConflict('a durable answer exists; resume it instead of repeating')


def resolve_model_call(queue, lease, dispatch, *, authorization=None):
    """Resolve one retained dispatch without transport or task settlement.

    Return (outcome, retained_event). This internal API does not establish operator
    consent: its entrypoint must supply explicit authorization and hold/renew the
    canonical owner and task lease. Existing decisions survive delivery changes.
    A recovered answer is a durable transport result, not a validated compile plan.
    """
    dispatch = progress._snapshot_event(dispatch)
    checkpoint = _retained_dispatch_checkpoint(queue, lease, dispatch)
    current = queue.get_compile_event(dispatch.work_id)
    if current is None:
        raise progress.ProgressCorrupt('retained dispatch has no current checkpoint')
    queue.verify_compile_checkpoint_owner(lease, current)
    resumed = _resolution_repeat(checkpoint, lease.id, dispatch, current, authorization)
    recorded = _recorded_repeat(queue, lease, current, resumed)
    if recorded is not None:
        return 'repeat_authorized', recorded
    return _resolve_or_authorize(queue, lease, dispatch, current, resumed)


def _retained_dispatch_checkpoint(queue, lease, dispatch):
    queue.verify_compile_checkpoint_owner(lease, dispatch)
    retained = queue.get_compile_event(dispatch.work_id, sequence=dispatch.sequence)
    if retained != dispatch:
        raise progress.ProgressConflict('resolution does not name the retained dispatch')
    checkpoint = _checkpoint_before_dispatch(queue, dispatch)
    expected = _dispatch_event(checkpoint, dispatch.task_id,
                               dispatch.details['model_call']['request_digest'])
    if expected != dispatch:
        raise progress.ProgressCorrupt('dispatch differs from its semantic checkpoint')
    return checkpoint


def _checkpoint_before_dispatch(queue, dispatch):
    progress._require_model_phase(dispatch)
    call = dispatch.details['model_call']
    if call['phase'] != 'dispatch':
        raise progress.ProgressConflict('resolution requires a dispatch event')
    checkpoint = queue.get_compile_event(dispatch.work_id, sequence=call['checkpoint_sequence'])
    if checkpoint is None:
        raise progress.ProgressCorrupt('dispatch semantic checkpoint is absent')
    _require_checkpoint(checkpoint)
    return checkpoint


def _resolution_repeat(checkpoint, task_id, dispatch, current, authorization):
    request_digest = dispatch.details['model_call']['request_digest']
    if authorization is None:
        authorization = _retained_repeat_authorization(current, request_digest)
    if authorization is None:
        return None
    resumed = _repeat_checkpoint(checkpoint, task_id, request_digest, authorization)
    _require_resolution_consent(dispatch, resumed.details['repeat_authorization'])
    return resumed


def _retained_repeat_authorization(current, request_digest):
    record = current.details.get('repeat_authorization')
    if record is None:
        return None
    if record.get('request_digest') != request_digest:
        raise progress.ProgressConflict('retained repeat belongs to another request')
    return {name: record[name] for name in ('dispatch_digest', 'actor', 'reason')}


def _require_resolution_consent(dispatch, authorization):
    if authorization['dispatch_digest'] != progress._event_digest(dispatch):
        raise progress.ProgressConflict('resolution consent names another dispatch')


def _resolve_or_authorize(queue, lease, dispatch, current, resumed):
    try:
        _resume_call(queue, lease, dispatch, current)
    except ProviderOutcomeUnknown:
        return _resolve_unknown_call(queue, lease, dispatch, resumed)
    answer = queue.get_compile_event(dispatch.work_id)
    _require_same_call(answer, dispatch)
    return 'answer_recovered', answer


def _resolve_unknown_call(queue, lease, dispatch, resumed):
    if resumed is None:
        return 'outcome_unknown', dispatch
    queue.record_compile_event(lease, resumed)
    return 'repeat_authorized', resumed


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


# Retain existing public class and serialized identities.
ProviderOutcomeUnknown.__module__ = "compile_controller"
CompileDelivery.__module__ = "compile_controller"
