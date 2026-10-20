"""Durable external-call boundary for the approved compile controller.

The candidate CLI/MCP/nightly paths use this controller. The shared client
prepares protected calls; semantic callers own work identity and publication.
Installation and full dependency/recovery qualification remain unfinished.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import zipfile
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import ExitStack, closing, contextmanager, nullcontext
from dataclasses import asdict, dataclass, replace
from dataclasses import field as dataclass_field
from datetime import datetime
from functools import partial
from itertools import groupby
from pathlib import Path

import compile_progress as progress
from bounded_io import IO_CHUNK_BYTES, VerifiedByteRange, require_byte_range
from context_budget import TokenCount, TokenUsage
from llm_client import BackendResponse, LLMResult, ProviderDescriptor
from reliable_memory import (
    canonical_json_bytes,
    fingerprint_regular_file,
    operational_deadline_active,
)

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
    from memory_queue import QueueFailure

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
    if current == resumed:
        queue.verify_compile_artifact(current)
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


def run_candidate_call(queue, lease, checkpoint, *, dependencies, descriptor,
                       prompt, system_prompt, delivery=None, **options):
    """Use the shared DLP/preparation path, journaling only external invocation.

    The semantic caller still owns the complete work/dependency identity. The
    protected-request hash alone does not bind source snapshots or compiler code.
    """
    from llm_client import call_candidate

    dependencies.verify()
    dispatch = partial(_journal_candidate, queue, lease, checkpoint, dependencies, descriptor, delivery)
    result = call_candidate(descriptor, prompt, system_prompt, dispatch=dispatch, **options)
    dependencies.verify()
    return result


def _journal_candidate(queue, lease, checkpoint, dependencies, descriptor, delivery, prepared):
    from reliable_memory import sha256_bytes

    request = sha256_bytes(canonical_json_bytes({
        'protected_request': prepared.request_digest, 'dependencies': dependencies.verify(),
    }))
    result = run_model_call(queue, lease, checkpoint, request_digest=request,
                           invoke=partial(_invoke_candidate, prepared, descriptor), delivery=delivery)
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
    Its caller supplies a verified snapshot checkpoint and a live dependency witness.
    """

    def __init__(self, queue, lease, checkpoint, *, dependencies, delivery=None):
        dependencies.verify()
        _require_checkpoint(checkpoint)
        self.queue = queue
        self.lease = lease
        self.checkpoint = progress._snapshot_event(checkpoint)
        self.dependencies = dependencies
        self.delivery = delivery
        self.probe_observations = []

    def __call__(self, descriptor, prompt, system_prompt, **options):
        result = run_candidate_call(
            self.queue, self.lease, self.checkpoint, dependencies=self.dependencies,
            descriptor=descriptor, prompt=prompt, system_prompt=system_prompt, delivery=self.delivery, **options,
        )
        if _is_completed_result(result):
            self.checkpoint = _advance_replay(self.queue, self.lease, self.checkpoint)
        return result

    def probe_candidate(self, descriptor, probe):
        self.dependencies.verify()
        self.checkpoint, available = _record_or_read_compile_probe(
            self.queue, self.lease, self.checkpoint, descriptor, probe, self.probe_observations)
        self.dependencies.verify()
        return available


def _record_or_read_compile_probe(queue, lease, checkpoint, descriptor, probe, observations):
    queue.verify_compile_checkpoint_owner(lease, checkpoint)
    saved = queue.get_compile_event(checkpoint.work_id, sequence=checkpoint.sequence + 1)
    if saved is not None:
        return _read_compile_probe(queue, lease, checkpoint, saved, descriptor, observations)
    observation = _compile_probe_observation(descriptor, probe(descriptor))
    observations.append(observation)
    if not observation['available']:
        return checkpoint, False
    event = _compile_probe_event(checkpoint, lease, observations)
    recorded = _record_or_read_compile_checkpoint(queue, lease, event)
    observations.clear()
    return recorded, True


def _read_compile_probe(queue, lease, checkpoint, saved, descriptor, observations):
    retained = _saved_probe_observations(saved)
    expected = _compile_probe_event(checkpoint, lease, retained)
    _matched_compile_checkpoint(queue, lease, saved, expected)
    observation = _probe_observation_at(retained, len(observations))
    if _compile_probe_observation(descriptor, observation['available']) != observation:
        raise progress.ProgressConflict('retained provider probe candidate changed')
    observations.append(observation)
    if not observation['available']:
        return checkpoint, False
    observations.clear()
    return saved, True


def _probe_observation_at(observations, index):
    if index >= len(observations):
        raise progress.ProgressConflict('retained provider probe order changed')
    return observations[index]


def _saved_probe_observations(event):
    observations = event.details.get('compile_probe', {}).get('observations')
    if not isinstance(observations, list) or not observations:
        raise progress.ProgressCorrupt('missing retained provider probes')
    for observation in observations:
        _require_probe_observation(observation)
    _require_probe_selection(observations)
    return observations


def _require_probe_selection(observations):
    if [item['available'] for item in observations] != [False] * (len(observations) - 1) + [True]:
        raise progress.ProgressCorrupt('invalid retained provider probe selection')


def _require_probe_observation(observation):
    if not isinstance(observation, dict) or set(observation) != {'descriptor_digest', 'available'}:
        raise progress.ProgressCorrupt('invalid retained provider probe fields')
    available = observation['available']
    if type(available) is not bool:
        raise progress.ProgressCorrupt('invalid retained provider probe result')
    progress._require_digest(observation['descriptor_digest'])


def _compile_probe_observation(descriptor, available):
    if type(available) is not bool:
        raise ValueError('provider probe must return a boolean')
    identity = {'provider': descriptor.canonical(), 'endpoint': descriptor._endpoint,
                'resolution_failure': descriptor.resolution_failure}
    return {'descriptor_digest': _compile_identity_digest(identity), 'available': available}


def _compile_probe_event(checkpoint, lease, observations):
    observation = {'schema': 'compile-provider-probe/v1',
                   'checkpoint_digest': progress._event_digest(checkpoint),
                   'observations': list(observations)}
    return replace(checkpoint, task_id=lease.id, sequence=checkpoint.sequence + 1,
                   details={'artifact': checkpoint.details['artifact'], 'compile_probe': observation})


def _advance_replay(queue, lease, checkpoint):
    answer = queue.get_compile_event(checkpoint.work_id, sequence=checkpoint.sequence + 2)
    if answer is None:
        return checkpoint  # A preflight refusal made no dispatch reservation.
    if answer.details.get('model_call', {}).get('phase') != 'answer':
        raise progress.ProgressConflict('replay requires a completed recorded call')
    queue.verify_compile_checkpoint_owner(lease, answer)
    queue.verify_compile_artifact(answer)
    following = replace(checkpoint, task_id=lease.id, sequence=answer.sequence + 1,
                        details={'artifact': checkpoint.details['artifact'],
                                 'replay': {'answer_digest': progress._event_digest(answer)}})
    return _record_or_read_compile_checkpoint(queue, lease, following)


def _record_or_read_compile_checkpoint(queue, lease, following):
    recorded = queue.get_compile_event(following.work_id, sequence=following.sequence)
    if recorded is None:
        queue.record_compile_event(lease, following)
        return following
    return _matched_compile_checkpoint(queue, lease, recorded, following)


def _matched_compile_checkpoint(queue, lease, recorded, following):
    queue.verify_compile_checkpoint_owner(lease, recorded)
    if replace(recorded, task_id=lease.id) != following:
        raise progress.ProgressConflict('compile checkpoint identity changed')
    return recorded


def _is_completed_result(result):
    return result.available is True and (
        result.text is not None or result.failure_class in _COMPLETED_REFUSALS
    )


def _snapshot_types():
    from compile_memory import DailySnapshot, SourceSnapshot, TargetSnapshot

    return {'dailies': DailySnapshot, 'sources': SourceSnapshot,
            'targets': TargetSnapshot, 'vault_files': SourceSnapshot}


def _require_snapshot_digest(content, digest):
    import hashlib

    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError('snapshot content digest mismatch')


def _require_original_digest(value):
    original = value.get('original_content')
    if original is not None and original != value['original_sha256']:
        raise ValueError('original snapshot content digest mismatch')


def _restore_snapshot_daily(value):
    from compile_memory import NativeCompileFrame

    value['original_entries'] = tuple(tuple(entry) for entry in value['original_entries'])
    value['native_frames'] = tuple(NativeCompileFrame(**frame) for frame in value['native_frames'])
    return value


def save_compile_inputs(queue, lease, checkpoint, inputs):
    """Write record-addressable inputs without a whole-archive bytes object."""
    _require_input_checkpoint(checkpoint)
    if compile_inputs_digest(inputs) != checkpoint.source_digest:
        raise ValueError('snapshot content digest mismatch')
    producer = partial(_write_archive_snapshot, inputs, checkpoint.source_digest)
    return queue.save_compile_artifact_writer(lease, checkpoint, producer)


def save_compile_capture(queue, lease, checkpoint, paths, *, compiled=None, deadline=None):
    """Save or recover the exact admitted source snapshot without materializing it."""
    checkpoint = _unbound_capture_checkpoint(checkpoint)
    _require_capture_parent(queue, lease, checkpoint)
    saved = _recorded_capture(queue, lease, checkpoint, deadline)
    if saved is not None:
        return saved
    validate = partial(_validate_capture_archive, checkpoint.source_digest, deadline)
    recovered = queue.recover_compile_artifact_validated(
        lease, checkpoint, validate=validate, deadline=deadline)
    if recovered is not None:
        return recovered
    producer = partial(_write_checked_capture, paths, checkpoint.source_digest, compiled)
    return queue.save_compile_artifact_writer(lease, checkpoint, producer, deadline=deadline)


@dataclass(frozen=True)
class CompileCapturePreparation:
    """Fresh local selection; consumed only when no retained capture exists."""
    paths: object
    dependencies: CompileDependencies
    request: dict
    compiled: object = None
    execution: dict | None = None


def admit_compile_capture(queue, lease, prepare, *, deadline=None):
    """Capture once, then atomically admit root and snapshot; never call a model."""
    operational_deadline_active(deadline)
    origin = queue.compile_origin(lease)
    root = queue.get_compile_event(progress.root_work_id(origin), sequence=0)
    if root is not None:
        return _resume_admitted_capture(queue, lease, root, origin, deadline)
    producer = partial(_write_admitted_capture, queue, lease, origin, prepare, deadline)
    decoder = partial(_read_admitted_capture_root, lease, origin, deadline)
    return queue.publish_initial_compile_capture(lease, producer=producer,
                                                 decode_root=decoder, deadline=deadline)


def _resume_admitted_capture(queue, lease, root, origin, deadline):
    progress.validate_root_event(root, origin)
    queue.verify_compile_checkpoint_owner(lease, root)
    checkpoint = replace(root, task_id=lease.id, sequence=1, stage='snapshot_ready', details={})
    saved = _recorded_capture(queue, lease, checkpoint, deadline)
    if saved is not None:
        return saved
    validate = partial(_validate_capture_archive, checkpoint.source_digest, deadline)
    recovered = queue.recover_compile_artifact_validated(lease, checkpoint, validate=validate, deadline=deadline)
    if recovered is None:
        raise progress.ProgressConflict('retained compile work has no recoverable source snapshot')
    return recovered


def _require_capture_vault(queue):
    from compile_memory import ROOT, STATE_ROOT

    expected = tuple(Path(path).resolve(strict=True) for path in (queue.vault, queue.state_root))
    actual = (Path(ROOT).resolve(strict=True), Path(STATE_ROOT).resolve(strict=True))
    if actual != expected:
        raise progress.ProgressConflict('capture vault identity differs from queue')


def _write_admitted_capture(queue, lease, origin, prepare, deadline, output):
    _require_capture_vault(queue)
    operational_deadline_active(deadline)
    prepared = prepare()
    binding = _capture_admission_binding(lease, origin, prepared)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        writer = _SnapshotArchiveWriter(archive)
        manifest = _capture_manifest(prepared.paths, prepared.compiled, writer.put, writer.write)
        prepared.dependencies.verify()
        manifest.update(schema=_ADMITTED_ARCHIVE_SCHEMA, admission=binding)
        writer.write('manifest.json', _snapshot_json(manifest))


def _capture_admission_binding(lease, origin, prepared):
    return {'schema': 'compile-capture-admission/v2', 'origin_task_id': origin,
            'input_hash': lease.input_hash, 'dependency_digest': prepared.dependencies.verify(),
            'request_digest': _compile_identity_digest(prepared.request),
            'request': prepared.request, 'execution': prepared.execution}


def read_compile_execution(queue, lease, snapshot, *, deadline=None):
    """Read execution inputs from the owned immutable admission, without discovery."""
    _require_preflight_parent(queue, lease, snapshot)
    with open_compile_inputs(queue, lease, snapshot, deadline=deadline) as reader:
        admission = _capture_admission_for_owner(reader.manifest, lease, queue.compile_origin(lease))
        if admission['schema'] != 'compile-capture-admission/v2' or admission['execution'] is None:
            raise progress.ProgressConflict('retained compile work has no execution planning evidence')
        result = admission['request'], admission['execution']
    queue.verify_compile_checkpoint_owner(lease, snapshot)
    operational_deadline_active(deadline)
    return result


def _read_admitted_capture_root(lease, origin, deadline, handle):
    operational_deadline_active(deadline)
    with zipfile.ZipFile(handle) as archive:
        _require_archive_members(archive)
        source_digest = hashlib.sha256(archive.read('manifest.json')).hexdigest()
        reader = CompileInputArchive(archive, source_digest)
        admission = _capture_admission_for_owner(reader.manifest, lease, origin)
        _validate_capture_reader(reader, deadline)
    details = progress.root_details(origin, source_digest, admission['dependency_digest'],
                                   admission['request_digest'])
    return progress.CompileEvent(progress.root_work_id(origin), lease.id, 0, source_digest,
                                 'pending', 'ready', {'compile_work': details})


def _capture_admission_for_owner(manifest, lease, origin):
    admission = manifest.get('admission')
    _require_capture_admission(admission)
    if (admission['origin_task_id'], admission['input_hash']) != (origin, lease.input_hash):
        raise progress.ProgressConflict('capture admission task or payload changed')
    return admission


def _require_capture_admission(admission):
    _require_capture_admission_fields(admission)
    progress.root_work_id(admission['origin_task_id'])
    for name in ('input_hash', 'dependency_digest', 'request_digest'):
        progress._require_digest(admission[name])
    if admission['schema'] == 'compile-capture-admission/v2':
        _require_capture_execution(admission)


def _require_capture_execution(admission):
    if _compile_identity_digest(admission['request']) != admission['request_digest']:
        raise progress.ProgressCorrupt('capture request digest changed')
    if admission['execution'] is not None and not isinstance(admission['execution'], dict):
        raise progress.ProgressCorrupt('invalid capture execution planning')


def _require_capture_admission_fields(admission):
    fields = {'schema', 'origin_task_id', 'input_hash', 'dependency_digest', 'request_digest'}
    versions = {'compile-capture-admission/v1': fields,
                'compile-capture-admission/v2': fields | {'request', 'execution'}}
    if not isinstance(admission, dict) or set(admission) != versions.get(admission.get('schema')):
        raise progress.ProgressCorrupt('invalid capture admission fields')


def _unbound_capture_checkpoint(checkpoint):
    checkpoint = progress._snapshot_event(checkpoint)
    _require_input_checkpoint(checkpoint)
    if checkpoint.sequence != 1 or 'artifact' in checkpoint.details:
        raise progress.ProgressConflict('capture requires the first unbound snapshot checkpoint')
    return checkpoint


def _require_capture_parent(queue, lease, checkpoint):
    parent = queue.get_compile_event(checkpoint.work_id, sequence=0)
    if parent is None:
        raise progress.ProgressConflict('capture requires durable pending work')
    queue.verify_compile_checkpoint_owner(lease, parent)
    if parent.stage != 'pending' or parent.source_digest != checkpoint.source_digest:
        raise progress.ProgressConflict('capture source identity differs from pending work')


def _recorded_capture(queue, lease, checkpoint, deadline):
    saved = queue.get_compile_event(checkpoint.work_id, sequence=checkpoint.sequence)
    if saved is None:
        return None
    queue.verify_compile_checkpoint_owner(lease, checkpoint)
    queue.verify_compile_checkpoint_owner(lease, saved)
    _require_capture_event_identity(checkpoint, saved)
    with queue.open_compile_artifact(saved, deadline=deadline) as handle:
        _validate_capture_archive(checkpoint.source_digest, deadline, handle)
    queue.verify_compile_checkpoint_owner(lease, saved)
    return saved


def _require_capture_event_identity(checkpoint, saved):
    details = dict(saved.details)
    details.pop('artifact', None)
    if replace(saved, task_id=checkpoint.task_id, details=details) != checkpoint:
        raise progress.ProgressConflict('capture checkpoint identity changed')


def _validate_capture_archive(expected, deadline, handle):
    operational_deadline_active(deadline)
    with zipfile.ZipFile(handle) as archive:
        reader = CompileInputArchive(archive, expected)
        _validate_capture_reader(reader, deadline)
    operational_deadline_active(deadline)


def _validate_capture_reader(reader, deadline):
    verified = set()
    for group in _snapshot_types():
        _validate_capture_records(reader, group, deadline, verified)
    operational_deadline_active(deadline)


def _validate_capture_records(reader, group, deadline, verified):
    signature = inspect.signature(_snapshot_types()[group])
    metadata = {}
    with closing(_capture_record_values(reader, group, deadline)) as records:
        for value, original in records:
            _validate_capture_record_bytes(reader, value, verified, deadline, original)
            metadata = _shared_record_blobs(value, _ARCHIVE_JSON_FIELDS, reader.json_blob, metadata)
            _validate_capture_record_shape(group, value, signature, metadata)


def _capture_record_values(reader, group, deadline):
    previous = (None, None)
    with ExitStack() as scope:
        for index in range(reader.record_count(group)):
            operational_deadline_active(deadline)
            value = reader.record_metadata(group, index)
            _require_original_digest(value)
            previous = _capture_original_range(reader, value, previous, scope, deadline)
            yield value, previous[1]


def _capture_original_range(reader, value, previous, scope, deadline):
    digest = value.get('original_content')
    if previous[0] == digest:
        return previous
    scope.close()
    if digest is None:
        return None, None
    return digest, scope.enter_context(reader.original_range(digest, deadline=deadline))


def _validate_capture_record_bytes(reader, value, verified, deadline, original):
    progress._require_digest(value['content'])
    if value['content'] != value['sha256']:
        raise ValueError('snapshot content digest mismatch')
    _validate_original_part(value, original, verified)
    for name in _ARCHIVE_BYTES_FIELDS:
        _validate_capture_blob(reader, value.get(name), verified, deadline)


def _validate_original_part(value, original, verified):
    if original is None:
        return
    start, end = value['byte_start'], value['byte_end']
    require_byte_range(start, end, len(original))
    actual = original.range_digest(start, end)
    if actual != value['sha256']:
        raise ValueError('snapshot part differs from original source range')
    verified.add(value['original_content'])


def _validate_capture_blob(reader, digest, verified, deadline):
    if digest is None:
        return
    progress._require_digest(digest)
    if digest not in verified:
        reader.verify_blob(digest, deadline=deadline)
        verified.add(digest)


def _stream_snapshot_digest(stream, deadline):
    digest = hashlib.sha256()
    while True:
        operational_deadline_active(deadline)
        chunk = stream.read(IO_CHUNK_BYTES)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _validate_capture_record_shape(group, value, signature, metadata):
    _archive_record_fields(value, _ARCHIVE_JSON_FIELDS, metadata.__getitem__)
    if group == 'dailies':
        value = _restore_snapshot_daily(value)
    signature.bind(**value)


def _write_checked_capture(paths, expected, compiled, output):
    actual = write_compile_capture(output, paths, compiled=compiled)
    if actual != expected:
        raise ValueError('captured snapshot digest changed before publication')


@contextmanager
def open_compile_inputs(queue, lease, checkpoint, *, deadline=None):
    """Own a verified snapshot reader; callers load only the records they need."""
    _require_input_checkpoint(checkpoint)
    queue.verify_compile_checkpoint_owner(lease, checkpoint)
    with queue.open_compile_artifact(checkpoint, deadline=deadline) as handle:
        with zipfile.ZipFile(handle) as archive:
            yield CompileInputArchive(archive, checkpoint.source_digest)


def save_compile_preflight(queue, lease, snapshot, *, dependencies, request, plan, deadline=None):
    """Persist a complete metadata plan against the immutable source snapshot.

    `plan(reader, on_refused)` yields CompileBatch values without model calls.
    The caller's dependency witness and request must bind its effective planning
    configuration. Recovery recomputes local planning from saved bytes, never
    mutable live sources; this API neither dispatches nor authorizes model work.
    """
    operational_deadline_active(deadline)
    snapshot = progress._snapshot_event(snapshot)
    root = _require_preflight_parent(queue, lease, snapshot)
    dependencies.verify()
    event = _preflight_event(lease, snapshot, dependencies, request)
    _require_preflight_work_identity(root, event)
    with open_compile_inputs(queue, lease, snapshot, deadline=deadline) as reader:
        produce = partial(_write_compile_preflight, reader, event.details, dependencies, plan, deadline)
        validate = partial(_validate_compile_preflight, produce, deadline)
        return _save_verified_compile_artifact(queue, lease, event, produce, validate, deadline)


def _require_preflight_parent(queue, lease, snapshot):
    _require_input_checkpoint(snapshot)
    queue.verify_compile_checkpoint_owner(lease, snapshot)
    stored = queue.get_compile_event(snapshot.work_id, sequence=snapshot.sequence)
    if stored != snapshot:
        raise progress.ProgressConflict('preflight requires the recorded source snapshot')
    root = queue.get_compile_event(snapshot.work_id, sequence=0)
    progress.validate_root_event(root, queue.compile_origin(lease))
    return root


def _require_preflight_work_identity(root, event):
    original = root.details['compile_work']
    current = event.details['compile_preflight']
    identities = ('dependency_digest', 'request_digest')
    if root.source_digest != event.source_digest:
        raise progress.ProgressConflict('preflight source identity changed')
    if any(original[name] != current[name] for name in identities):
        raise progress.ProgressConflict('preflight checkpoint identity changed')


def _preflight_event(lease, snapshot, dependencies, request):
    binding = {'schema': 'compile-preflight/v1',
               'snapshot_digest': progress._event_digest(snapshot),
               'dependency_digest': dependencies.digest,
               'request_digest': _compile_identity_digest(request)}
    return replace(snapshot, task_id=lease.id, sequence=snapshot.sequence + 1,
                   stage='pending', outcome='ready', details={'compile_preflight': binding})


def _save_verified_compile_artifact(queue, lease, event, produce, validate, deadline):
    saved = queue.get_compile_event(event.work_id, sequence=event.sequence)
    if saved is not None:
        _verify_saved_compile_artifact(queue, lease, event, saved, validate, deadline)
        return saved
    recovered = queue.recover_compile_artifact_validated(lease, event, validate=validate, deadline=deadline)
    if recovered is not None:
        return recovered
    return queue.save_compile_artifact_writer(lease, event, produce, deadline=deadline)


def _verify_saved_compile_artifact(queue, lease, event, saved, validate, deadline):
    queue.verify_compile_checkpoint_owner(lease, saved)
    details = dict(saved.details)
    details.pop('artifact', None)
    if replace(saved, task_id=event.task_id, details=details) != event:
        raise progress.ProgressConflict('preflight checkpoint identity changed')
    with queue.open_compile_artifact(saved, deadline=deadline) as handle:
        validate(handle)
    queue.verify_compile_checkpoint_owner(lease, saved)


class _PreflightDigest:
    def __init__(self):
        self.digest = hashlib.sha256()

    def write(self, content):
        self.digest.update(content)
        return len(content)


def _validate_compile_preflight(produce, deadline, handle):
    expected = _PreflightDigest()
    produce(expected)
    actual = hashlib.sha256()
    # Transfer granularity only: no artifact-size or record-count ceiling.
    for chunk in iter(partial(handle.read, 65536), b''):
        operational_deadline_active(deadline)
        actual.update(chunk)
    if actual.digest() != expected.digest.digest():
        raise progress.ProgressCorrupt('preflight differs from complete saved-source planning')


class _PreflightWriter:
    def __init__(self, reader, output, deadline):
        self.deadline = deadline
        self.remaining = Counter(reader.manifest['records']['dailies'])
        self.output = output
        self.counts = Counter()

    def write(self, value):
        operational_deadline_active(self.deadline)
        data = _snapshot_json(value)
        self.output.write(data + b'\n')
        return _blob_digest(data)

    def add(self, kind, inputs, details):
        records = _preflight_source_records(inputs)
        self._consume(records)
        identity = self.write({'kind': kind, 'source_records': records, **details})
        self.counts[kind] += 1
        return identity

    def _consume(self, records):
        if not records:
            raise ValueError('preflight entry has no source records')
        self.remaining.subtract(records)
        if any(self.remaining[record] < 0 for record in records):
            raise ValueError('preflight repeats or changes a source record')

    def refused(self, inputs):
        self.add('refused', inputs, {'reason': 'input_capacity'})

    def complete(self):
        if any(self.remaining.values()):
            raise ValueError('preflight omitted source records')
        self.write({'kind': 'complete', 'counts': dict(self.counts)})


def _preflight_batch_details(batch):
    sources = [_archive_record_value(source, _blob_digest) for source in batch.inputs.sources]
    return {'manifest': [source.receipt_descriptor() for source in batch.manifest],
            'manifest_sha256': batch.manifest_sha256,
            'packing': batch.packing.canonical(), 'planning_model': batch.planning_model,
            'context_pending': batch.context_pending,
            'required_context_paths': list(batch.required_context_paths),
            'sources': sources}


def _write_compile_preflight(reader, binding, dependencies, plan, deadline, output):
    writer = _PreflightWriter(reader, output, deadline)
    writer.write({'kind': 'header', **binding})
    for batch in plan(reader, writer.refused):
        writer.add('batch', batch.inputs, _preflight_batch_details(batch))
    dependencies.verify()
    writer.complete()


@dataclass(frozen=True)
class PlannedCompileBatch:
    """Verified planning identity, not admission for model calls or publication.

    The batch borrows lazy records from the iterator's open archive. Access those
    records while the iterator is open; ordinary consumers process one batch at
    a time to avoid retaining completed source bodies.
    """
    batch: object
    preflight: progress.CompileEvent
    entry_digest: str
    work_id: str
    entry_offset: int


class _PreflightMatch:
    def __init__(self, handle):
        self.handle = handle

    def write(self, content):
        if self.handle.read(len(content)) != content:
            raise progress.ProgressCorrupt('preflight replay record changed')
        return len(content)

    def finish(self):
        if self.handle.read(1):
            raise progress.ProgressCorrupt('preflight replay has trailing records')


def iter_compile_preflight(queue, lease, snapshot, *, dependencies, request, plan,
                           on_refused, deadline=None):
    """Yield verified batches only after a complete preflight is durable.

    Reuse the existing writer as an exact-byte comparator instead of maintaining
    a second plan decoder. Refusal callbacks run only during checked consumption,
    not while saving/validating the complete preflight. Close on early exit.
    """
    saved = save_compile_preflight(queue, lease, snapshot, dependencies=dependencies,
        request=request, plan=plan, deadline=deadline)
    yield from _iter_saved_compile_preflight(queue, lease, snapshot, saved, dependencies,
                                             plan, on_refused, deadline)


def _iter_saved_compile_preflight(queue, lease, snapshot, saved, dependencies, plan, on_refused, deadline):
    check = partial(_check_preflight_consumer, queue, lease, saved, dependencies, deadline)
    with open_compile_inputs(queue, lease, snapshot, deadline=deadline) as reader:
        with queue.open_compile_artifact(saved, deadline=deadline) as handle:
            yield from _consume_compile_preflight(reader, handle, saved, plan, on_refused, check, deadline)


def _check_preflight_consumer(queue, lease, saved, dependencies, deadline):
    operational_deadline_active(deadline)
    dependencies.verify()
    queue.verify_compile_checkpoint_owner(lease, saved)
    operational_deadline_active(deadline)


def _consume_compile_preflight(reader, handle, saved, plan, on_refused, check, deadline):
    matcher = _PreflightMatch(handle)
    writer = _PreflightWriter(reader, matcher, deadline)
    writer.write({'kind': 'header', 'compile_preflight': saved.details['compile_preflight']})
    refused = partial(_consume_preflight_refusal, writer, check, on_refused)
    for batch in plan(reader, refused):
        entry_offset = handle.tell()
        entry_digest = writer.add('batch', batch.inputs, _preflight_batch_details(batch))
        check()
        yield _planned_compile_batch(batch, saved, entry_digest, entry_offset)
    check()
    writer.complete()
    matcher.finish()


def _consume_preflight_refusal(writer, check, on_refused, inputs):
    writer.refused(inputs)
    check()
    if on_refused is not None:
        on_refused(inputs)


def _planned_compile_batch(batch, preflight, entry_digest, entry_offset):
    identity = {'schema': 'compile-planned-batch/v1',
                'preflight_digest': progress._event_digest(preflight),
                'entry_digest': entry_digest}
    return PlannedCompileBatch(batch, progress._snapshot_event(preflight), entry_digest,
                               _compile_identity_digest(identity), entry_offset)


def _preflight_source_records(inputs):
    return [_blob_digest(_snapshot_json(_archive_record_value(part, _blob_digest)))
            for part in inputs.dailies]


def _planned_batch_record(planned):
    value = {'kind': 'batch', 'source_records': _preflight_source_records(planned.batch.inputs),
             **_preflight_batch_details(planned.batch)}
    data = _snapshot_json(value)
    if _blob_digest(data) != planned.entry_digest:
        raise progress.ProgressConflict('planned batch entry identity changed')
    return data


def _require_recorded_batch_parent(queue, lease, planned, dependencies):
    parent = planned.preflight
    queue.verify_compile_checkpoint_owner(lease, parent)
    stored = queue.get_compile_event(parent.work_id, sequence=parent.sequence)
    if stored != parent:
        raise progress.ProgressConflict('batch requires the recorded preflight')
    root = queue.get_compile_event(parent.work_id, sequence=0)
    progress.validate_root_event(root, queue.compile_origin(lease))
    _require_preflight_work_identity(root, parent)
    if dependencies.verify() != parent.details['compile_preflight']['dependency_digest']:
        raise progress.ProgressConflict('batch dependency identity changed')


def _require_planned_batch_address(planned):
    expected = _planned_compile_batch(planned.batch, planned.preflight,
                                      planned.entry_digest, planned.entry_offset)
    if expected.work_id != planned.work_id:
        raise progress.ProgressConflict('planned batch work identity changed')
    if type(planned.entry_offset) is not int or planned.entry_offset < 0:
        raise progress.ProgressCorrupt('invalid preflight entry offset')


def _require_planned_batch_record(queue, planned, deadline):
    _require_planned_batch_address(planned)
    data = _planned_batch_record(planned)
    with queue.open_compile_artifact(planned.preflight, deadline=deadline) as handle:
        handle.seek(planned.entry_offset)
        if handle.read(len(data) + 1) != data + b'\n':
            raise progress.ProgressCorrupt('planned batch is absent from retained preflight')


def _batch_admission_event(lease, planned, dependencies):
    parent = planned.preflight
    details = {'schema': 'compile-batch/v1', 'preflight_work_id': parent.work_id,
               'preflight_sequence': parent.sequence,
               'preflight_digest': progress._event_digest(parent),
               'entry_digest': planned.entry_digest, 'entry_offset': planned.entry_offset,
               'dependency_digest': dependencies.digest}
    sources = _compile_identity_digest(_preflight_source_records(planned.batch.inputs))
    return progress.CompileEvent(planned.work_id, lease.id, 0, sources,
                                 'pending', 'ready', {'compile_batch': details,
                                 'source_scope': _batch_source_scope(planned.batch)})


def _batch_source_scope(batch):
    return progress.make_source_scope(_batch_source_span(part) for part in batch.inputs.dailies)


def _batch_source_span(part):
    from compile_memory import _v4_source_descriptor

    source = _v4_source_descriptor(part)
    return {key: source[key] for key in ('logical_path', 'byte_start', 'byte_end', 'sha256')}


def begin_compile_batch(queue, lease, planned, *, dependencies, deadline=None):
    """Admit a retained plan entry before reading any mutable context.

    This binds immutable daily records and the complete preflight. It does not
    authorize model calls or certify that refreshed context has been saved.
    """
    operational_deadline_active(deadline)
    _require_recorded_batch_parent(queue, lease, planned, dependencies)
    _require_planned_batch_record(queue, planned, deadline)
    expected = _batch_admission_event(lease, planned, dependencies)
    dependencies.verify()
    operational_deadline_active(deadline)
    return _record_or_resume_batch_admission(queue, lease, expected)


def _record_or_resume_batch_admission(queue, lease, expected):
    previous = queue.get_compile_event(expected.work_id, sequence=0)
    if previous is not None:
        return _resume_batch_admission(queue, lease, previous, expected)
    queue.record_compile_event(lease, expected)
    return expected


def _resume_batch_admission(queue, lease, previous, expected):
    queue.verify_compile_checkpoint_owner(lease, previous)
    if replace(previous, task_id=lease.id) != expected:
        raise progress.ProgressConflict('retained batch admission identity changed')
    return previous


def _batch_context_event(queue, lease, planned, snapshot, dependencies, deadline):
    admission = begin_compile_batch(queue, lease, planned, dependencies=dependencies, deadline=deadline)
    _require_preflight_parent(queue, lease, snapshot)
    if planned.preflight.details['compile_preflight']['snapshot_digest'] != progress._event_digest(snapshot):
        raise progress.ProgressConflict('batch context source snapshot changed')
    binding = {'schema': 'compile-batch-context/v1',
               'admission_digest': progress._event_digest(admission),
               'snapshot_work_id': snapshot.work_id, 'snapshot_sequence': snapshot.sequence,
               'snapshot_digest': progress._event_digest(snapshot)}
    return replace(admission, task_id=lease.id, sequence=1, stage='snapshot_ready',
                   details={'compile_context': binding})


def _context_event_binding(event):
    return _compile_identity_digest(event.details['compile_context'])


def _capture_current_compile_context():
    from compile_memory import iter_compile_records

    return iter_compile_records(())


def save_compile_batch_context(queue, lease, planned, snapshot, *, dependencies,
                               capture=_capture_current_compile_context, deadline=None):
    """Recover exact context before invoking the live capture producer."""
    event = _batch_context_event(queue, lease, planned, snapshot, dependencies, deadline)
    with open_compile_inputs(queue, lease, snapshot, deadline=deadline) as base:
        binding = _context_event_binding(event)
        produce = partial(_write_batch_context, base, binding, dependencies, capture, deadline)
        validate = partial(_validate_batch_context, base, binding, dependencies, deadline)
        saved = _save_verified_compile_artifact(queue, lease, event, produce, validate, deadline)
    dependencies.verify()
    queue.verify_compile_checkpoint_owner(lease, saved)
    return saved


def _write_batch_context(base, binding, dependencies, capture, deadline, output):
    dependencies.verify()
    write_compile_context_delta(output, base, capture(), binding=binding, deadline=deadline)
    dependencies.verify()


def _read_bound_batch_context(archive, base, binding):
    manifest_digest = _blob_digest(archive.read('manifest.json'))
    return read_compile_context_delta(archive, manifest_digest, base, binding=binding)


def _validate_context_view(view, deadline):
    for group in _CONTEXT_GROUPS:
        for _record in getattr(view, group):
            operational_deadline_active(deadline)


def _validate_batch_context(base, binding, dependencies, deadline, handle):
    with zipfile.ZipFile(handle) as archive:
        view = _read_bound_batch_context(archive, base, binding)
        _validate_context_view(view, deadline)
    dependencies.verify()
    operational_deadline_active(deadline)


@contextmanager
def open_compile_batch_context(queue, lease, planned, snapshot, saved, *, dependencies, deadline=None):
    """Yield the full saved context under both archive lifetimes and owner checks."""
    expected = _batch_context_event(queue, lease, planned, snapshot, dependencies, deadline)
    recorded = queue.get_compile_event(expected.work_id, sequence=expected.sequence)
    if recorded != saved:
        raise progress.ProgressConflict('batch context requires the recorded checkpoint')
    _require_capture_event_identity(expected, saved)
    with open_compile_inputs(queue, lease, snapshot, deadline=deadline) as base:
        with queue.open_compile_artifact(saved, deadline=deadline) as handle:
            with zipfile.ZipFile(handle) as archive:
                view = _read_bound_batch_context(archive, base, _context_event_binding(expected))
                _validate_context_view(view, deadline)
                _check_batch_context_owner(queue, lease, saved, dependencies, deadline)
                yield view
                _check_batch_context_owner(queue, lease, saved, dependencies, deadline)


@contextmanager
def open_prepared_compile_batch(queue, lease, planned, snapshot, *, dependencies,
                                capture=_capture_current_compile_context, deadline=None):
    """Prepare from retained context while its lazy source/target views stay open."""
    from compile_memory import refresh_compile_batch_from_context

    saved = save_compile_batch_context(queue, lease, planned, snapshot,
        dependencies=dependencies, capture=capture, deadline=deadline)
    with open_compile_batch_context(queue, lease, planned, snapshot, saved,
                                    dependencies=dependencies, deadline=deadline) as context:
        compiler_deadline = float('inf') if deadline is None else deadline
        ready = refresh_compile_batch_from_context(planned.batch, context, deadline=compiler_deadline)
        _check_batch_context_owner(queue, lease, saved, dependencies, deadline)
        yield ready, saved


def execute_planned_compile_batch(queue, lease, planned, snapshot, *, coordinator, dependencies,
                                  trigger, owner=None, deadline=None, cancelled=None, delivery=None,
                                  capture=_capture_current_compile_context):
    """Resolve and publish one retained batch; never settle its enclosing task.

    Repair-capacity and unknown-outcome errors propagate to the delivery owner.
    The worker remains responsible for whole-call process supervision.
    """
    from compile_memory import _require_compile_active

    _require_publication_coordinator(queue, coordinator)
    compiler_deadline = float('inf') if deadline is None else deadline
    _require_compile_active(compiler_deadline, cancelled)
    if delivery is not None:
        delivery.require_owner(queue, lease)
    with open_prepared_compile_batch(queue, lease, planned, snapshot, dependencies=dependencies,
                                     capture=capture, deadline=deadline) as (batch, context):
        return _execute_context_compile_batch(queue, lease, context, batch, coordinator=coordinator,
            dependencies=dependencies, trigger=trigger, owner=owner, deadline=deadline,
            cancelled=cancelled, delivery=delivery, critic_feedback=())


def execute_planned_compile_tree(queue, lease, planned, snapshot, *, coordinator, dependencies,
                                 trigger, owner=None, deadline=None, cancelled=None, delivery=None,
                                 capture=_capture_current_compile_context):
    """Own the retained archive while executing a batch and its saved repairs."""
    from compile_memory import _require_compile_active

    _require_publication_coordinator(queue, coordinator)
    _require_compile_active(float('inf') if deadline is None else deadline, cancelled)
    if delivery is not None:
        delivery.require_owner(queue, lease)
    with open_prepared_compile_batch(queue, lease, planned, snapshot, dependencies=dependencies,
                                     capture=capture, deadline=deadline) as (batch, context):
        return execute_compile_tree(queue, lease, context, batch, coordinator=coordinator,
            dependencies=dependencies, trigger=trigger, owner=owner, deadline=deadline,
            cancelled=cancelled, delivery=delivery)


def execute_compile_tree(queue, lease, context, batch, *, coordinator, dependencies, trigger,
                         owner=None, deadline=None, cancelled=None, delivery=None):
    """Execute disjoint retained source branches; incomplete work never proves success.

    The caller owns the open archive. An owned delivery blocks on unknown calls
    after independent siblings; a supplied delivery retains that obligation.
    Capacity refusals and recorded resolution failures return no completion.
    Other failures propagate. This does not settle the enclosing queue task.
    """
    _require_publication_coordinator(queue, coordinator)
    scope = CompileDelivery(queue, lease) if delivery is None else nullcontext(delivery)
    with scope as active:
        active.require_owner(queue, lease)
        return _CompileTreeExecution(queue, lease, coordinator, dependencies, trigger,
            owner, deadline, cancelled, active).run(context, batch)


@dataclass(frozen=True)
class _CompileTreeFrame:
    context: progress.CompileEvent
    batch: object
    feedback: tuple = ()
    child: RepairCompileBatch | None = None


@dataclass
class _CompileTreeExecution:
    queue: object
    lease: object
    coordinator: object
    dependencies: object
    trigger: str
    owner: object
    deadline: float | None
    cancelled: object
    delivery: CompileDelivery

    def run(self, context, batch):
        stack = [_CompileTreeFrame(context, batch)]
        while stack:
            self._check_active()
            frame = self._admit(stack.pop())
            self._visit(frame, stack)
        return self._confirm(context, batch)

    def _check_active(self):
        from compile_memory import _require_compile_active

        _require_compile_active(float('inf') if self.deadline is None else self.deadline, self.cancelled)

    def _admit(self, frame):
        if frame.child is None:
            return frame
        context = begin_compile_repair_batch(self.queue, self.lease, frame.child, frame.context, frame.batch,
            dependencies=self.dependencies, deadline=self.deadline)
        return _CompileTreeFrame(context, frame.child.batch, frame.child.feedback)

    def _visit(self, frame, stack):
        from compile_memory import CompilePlanUnavailable, _CompileRepairCapacityError

        try:
            _execute_context_compile_batch(self.queue, self.lease, frame.context, frame.batch,
                coordinator=self.coordinator, dependencies=self.dependencies, trigger=self.trigger,
                owner=self.owner, deadline=self.deadline, cancelled=self.cancelled,
                delivery=self.delivery, critic_feedback=frame.feedback)
        except _CompileRepairCapacityError:
            self._expand(frame, stack)
        except ProviderOutcomeUnknown:
            # CompileDelivery retains uncertainty; final proof cannot complete this leaf.
            return
        except CompilePlanUnavailable as failure:
            self._record_resolution_failure(frame, failure)

    def _record_resolution_failure(self, frame, failure):
        from compile_memory import _record_failed_batch

        self._check_active()
        _check_batch_context_owner(self.queue, self.lease, frame.context,
                                   self.dependencies, self.deadline)
        _record_failed_batch(frame.batch.inputs, failure,
                             deadline=float('inf') if self.deadline is None else self.deadline)

    def _expand(self, frame, stack):
        repair = _compile_work_cursor(self.queue, frame.context.work_id)
        children = read_compile_repair(self.queue, self.lease, repair, frame.context, frame.batch,
            dependencies=self.dependencies, deadline=self.deadline)
        for child in reversed(children):
            stack.append(_CompileTreeFrame(frame.context, frame.batch, child=child))

    def _confirm(self, context, batch):
        self._check_active()
        cursor = _compile_work_cursor(self.queue, context.work_id)
        options = dict(coordinator=self.coordinator, dependencies=self.dependencies,
                       owner=self.owner, deadline=self.deadline, cancelled=self.cancelled)
        if 'compile_repair' in cursor.details:
            return confirm_compile_repair(self.queue, self.lease, cursor, context, batch, **options)
        if cursor.stage == 'publication_ready':
            return confirm_compile_publication(self.queue, self.lease, cursor, batch, **options)
        return None


def _resolve_compile_publication(queue, lease, context, batch, *, coordinator, dependencies,
                                 delivery, deadline, critic_feedback=()):
    current = _compile_work_cursor(queue, context.work_id)
    if current.stage == 'publication_ready':
        return current
    if 'compile_repair' in current.details:
        return _raise_retained_compile_repair(queue, lease, current, context, batch, dependencies, deadline)
    return _resolve_new_compile_publication(queue, lease, context, batch, coordinator,
                                            dependencies, delivery, deadline, critic_feedback)


def _resolve_new_compile_publication(queue, lease, context, batch, coordinator, dependencies,
                                     delivery, deadline, critic_feedback):
    from compile_cache import CompileCache
    from compile_memory import _CompileRepairCapacityError, resolve_compile_plan

    replay = CompileReplay(queue, lease, context, dependencies=dependencies, delivery=delivery)
    try:
        resolved = resolve_compile_plan(batch.inputs, CompileCache(queue.state_root),
            coordinator=coordinator, batch=batch, call_runner=replay, critic_feedback=critic_feedback)
    except _CompileRepairCapacityError as failure:
        save_compile_repair(queue, lease, context, batch, failure, dependencies=dependencies, deadline=deadline)
        raise
    return save_compile_publication(queue, lease, context, batch, resolved,
                                    dependencies=dependencies, deadline=deadline)


@dataclass(frozen=True)
class RepairCompileBatch:
    """A checked partition borrowing its parent's open context, not call admission."""

    batch: object
    feedback: tuple[str, ...]
    repair: progress.CompileEvent
    entry_digest: str
    work_id: str


def _repair_failure_content(failure):
    from compile_memory import _CompileRepairCapacityError

    if not isinstance(failure, _CompileRepairCapacityError):
        raise TypeError('repair requires a compile capacity failure')
    content = {'reason': str(failure), 'feedback': list(failure.feedback)}
    _require_repair_failure_content(content)
    return content


def _require_repair_failure_content(content):
    if not isinstance(content, dict) or set(content) != {'reason', 'feedback'}:
        raise progress.ProgressCorrupt('invalid repair failure fields')
    if not isinstance(content['reason'], str) or not isinstance(content['feedback'], list):
        raise progress.ProgressCorrupt('invalid repair failure values')
    _require_repair_feedback_strings(content['feedback'])


def _require_repair_feedback_strings(feedback):
    if any(not isinstance(item, str) for item in feedback):
        raise progress.ProgressCorrupt('repair feedback must retain strings')


def _repair_binding(context, batch, failure, dependencies):
    return {'schema': 'compile-repair/v1', 'context_sequence': context.sequence,
            'context_digest': progress._event_digest(context),
            'batch_digest': _compile_identity_digest(_preflight_batch_details(batch)),
            'failure_digest': _compile_identity_digest(failure), 'dependency_digest': dependencies.digest}


def _repair_checkpoint(queue, lease, context, binding):
    latest = _compile_work_cursor(queue, context.work_id)
    queue.verify_compile_checkpoint_owner(lease, latest)
    if 'compile_repair' in latest.details:
        return _existing_repair_checkpoint(latest, binding)
    _require_checkpoint(latest)
    if latest.details['artifact'] != context.details['artifact']:
        raise progress.ProgressConflict('repair cursor lost its context artifact')
    return replace(context, task_id=lease.id, sequence=latest.sequence + 1,
                   stage='review_ready', outcome='waiting', details={'compile_repair': binding})


def _existing_repair_checkpoint(event, binding):
    if (event.stage, event.outcome, event.details.get('compile_repair')) != ('review_ready', 'waiting', binding):
        raise progress.ProgressConflict('repair decision identity changed')
    details = dict(event.details)
    details.pop('artifact', None)
    return replace(event, details=details)


def _require_repair_partition(batch, children):
    if not children:
        return
    remaining = Counter(_preflight_source_records(batch.inputs))
    for child, _feedback in children:
        _require_repair_child(batch, child)
        remaining.subtract(_preflight_source_records(child.inputs))
    if any(remaining.values()):
        raise progress.ProgressCorrupt('repair source coverage changed')


def _require_repair_child(parent, child):
    from compile_memory import _require_repair_context

    if not 0 < len(child.inputs.dailies) < len(parent.inputs.dailies):
        raise progress.ProgressCorrupt('repair child does not reduce complete source work')
    _require_repair_context(parent, child)
    if (child.inputs.targets, child.inputs.vault_files) != (parent.inputs.targets, parent.inputs.vault_files):
        raise progress.ProgressCorrupt('repair child changed publication context')


def _repair_child_record(child, feedback):
    return {'source_records': _preflight_source_records(child.inputs),
            **_preflight_batch_details(child), 'feedback': list(feedback)}


def _repair_document(event, batch, failure):
    from compile_memory import _repartition_compile_repair

    children = _repartition_compile_repair(batch, tuple(failure['feedback']))
    _require_repair_partition(batch, children)
    records = [_repair_child_record(child, feedback) for child, feedback in children]
    document = {'schema': 'compile-repair-document/v1', 'binding': event.details['compile_repair'],
                'failure': failure, 'children': records}
    return document, children


def _write_repair_document(document, dependencies, deadline, output):
    dependencies.verify()
    operational_deadline_active(deadline)
    output.write(_snapshot_json(document))
    dependencies.verify()
    operational_deadline_active(deadline)


def _validate_repair_document(document, dependencies, deadline, handle):
    if handle.read() != _snapshot_json(document):
        raise progress.ProgressCorrupt('retained repair partition changed')
    dependencies.verify()
    operational_deadline_active(deadline)


def save_compile_repair(queue, lease, context, batch, failure, *, dependencies, deadline=None):
    """Retain a capacity repair decision before admitting any child model call."""
    operational_deadline_active(deadline)
    _require_publication_context(queue, lease, context, batch, dependencies)
    content = _repair_failure_content(failure)
    binding = _repair_binding(context, batch, content, dependencies)
    event = _repair_checkpoint(queue, lease, context, binding)
    document, _children = _repair_document(event, batch, content)
    saved = _save_verified_compile_artifact(queue, lease, event,
        partial(_write_repair_document, document, dependencies, deadline),
        partial(_validate_repair_document, document, dependencies, deadline), deadline)
    _check_batch_context_owner(queue, lease, saved, dependencies, deadline)
    return saved


def _decoded_repair_document(event, raw):
    document = json.loads(raw)
    fields = {'schema', 'binding', 'failure', 'children'}
    if not isinstance(document, dict) or set(document) != fields:
        raise progress.ProgressCorrupt('invalid repair document fields')
    expected = ('compile-repair-document/v1', event.details['compile_repair'], raw)
    if (document['schema'], document['binding'], _snapshot_json(document)) != expected:
        raise progress.ProgressCorrupt('repair document identity changed')
    _require_repair_failure_content(document['failure'])
    return document


def _repair_child_identity(event, batch, feedback, record):
    digest = _compile_identity_digest(record)
    work_id = _compile_identity_digest({'schema': 'compile-repair-child/v1',
        'repair_digest': progress._event_digest(event), 'entry_digest': digest})
    return RepairCompileBatch(batch, tuple(feedback), progress._snapshot_event(event), digest, work_id)


def read_compile_repair(queue, lease, event, context, batch, *, dependencies, deadline=None):
    """Reconstruct checked children while the caller keeps the parent context open."""
    document, children = _read_verified_compile_repair(queue, lease, event, context, batch, dependencies, deadline)
    return tuple(_repair_child_identity(event, child, feedback, record)
                 for (child, feedback), record in zip(children, document['children']))


def _raise_retained_compile_repair(queue, lease, event, context, batch, dependencies, deadline):
    from compile_memory import _CompileRepairCapacityError

    document, _children = _read_verified_compile_repair(queue, lease, event, context, batch, dependencies, deadline)
    raise _CompileRepairCapacityError(document['failure']['reason'], tuple(document['failure']['feedback']))


def _read_verified_compile_repair(queue, lease, event, context, batch, dependencies, deadline):
    _require_publication_context(queue, lease, context, batch, dependencies)
    _require_recorded_repair(queue, lease, event)
    with queue.open_compile_artifact(event, deadline=deadline) as handle:
        document = _decoded_repair_document(event, handle.read())
    _existing_repair_checkpoint(event, _repair_binding(context, batch, document['failure'], dependencies))
    expected, children = _repair_document(event, batch, document['failure'])
    if document != expected:
        raise progress.ProgressCorrupt('retained repair partition changed')
    _check_batch_context_owner(queue, lease, event, dependencies, deadline)
    return document, children


def _require_recorded_repair(queue, lease, event):
    queue.verify_compile_checkpoint_owner(lease, event)
    if queue.get_compile_event(event.work_id, sequence=event.sequence) != event:
        raise progress.ProgressConflict('repair requires a recorded decision')
    _existing_repair_checkpoint(event, event.details['compile_repair'])


def _require_repair_child_record(child, document, parent_batch):
    _require_repair_child(parent_batch, child.batch)
    record = _repair_child_record(child.batch, child.feedback)
    expected = _repair_child_identity(child.repair, child.batch, child.feedback, record)
    if (child.work_id, child.entry_digest) != (expected.work_id, expected.entry_digest):
        raise progress.ProgressConflict('repair child identity changed')
    if record not in document['children']:
        raise progress.ProgressConflict('repair child is absent from retained decision')


def _repair_child_admission(lease, child, dependencies):
    parent = child.repair
    binding = {'schema': 'compile-repair-batch/v1', 'repair_work_id': parent.work_id,
               'repair_sequence': parent.sequence, 'repair_digest': progress._event_digest(parent),
               'entry_digest': child.entry_digest, 'dependency_digest': dependencies.digest}
    source_digest = _compile_identity_digest(_preflight_source_records(child.batch.inputs))
    return progress.CompileEvent(child.work_id, lease.id, 0, source_digest,
                                 'pending', 'ready', {'compile_batch': binding,
                                 'source_scope': _batch_source_scope(child.batch)})


def _repair_child_context(lease, admission, parent_context):
    binding = {'schema': 'compile-repair-context/v1',
               'admission_digest': progress._event_digest(admission),
               'parent_work_id': parent_context.work_id, 'parent_sequence': parent_context.sequence,
               'parent_digest': progress._event_digest(parent_context)}
    return replace(admission, task_id=lease.id, sequence=1, stage='snapshot_ready', details={
        'compile_context': parent_context.details['compile_context'], 'repair_context': binding,
        'artifact': parent_context.details['artifact']})


def begin_compile_repair_batch(queue, lease, child, parent_context, parent_batch, *, dependencies, deadline=None):
    """Bind child authority to the exact retained decision and inherited context."""
    operational_deadline_active(deadline)
    document, _children = _read_verified_compile_repair(queue, lease, child.repair,
        parent_context, parent_batch, dependencies, deadline)
    _require_repair_child_record(child, document, parent_batch)
    expected = _repair_child_admission(lease, child, dependencies)
    _check_batch_context_owner(queue, lease, child.repair, dependencies, deadline)
    admitted = _record_or_resume_batch_admission(queue, lease, expected)
    context = _repair_child_context(lease, admitted, parent_context)
    saved = _record_or_read_compile_checkpoint(queue, lease, context)
    _check_batch_context_owner(queue, lease, saved, dependencies, deadline)
    return saved


def execute_repair_compile_batch(queue, lease, child, parent_context, parent_batch, *, coordinator,
                                 dependencies, trigger, owner=None, deadline=None, cancelled=None, delivery=None):
    """Execute one admitted child while its caller owns the parent archive scope."""
    from compile_memory import _require_compile_active

    _require_publication_coordinator(queue, coordinator)
    compiler_deadline = float('inf') if deadline is None else deadline
    _require_compile_active(compiler_deadline, cancelled)
    if delivery is not None:
        delivery.require_owner(queue, lease)
    context = begin_compile_repair_batch(queue, lease, child, parent_context, parent_batch,
                                         dependencies=dependencies, deadline=deadline)
    return _execute_context_compile_batch(queue, lease, context, child.batch, coordinator=coordinator,
        dependencies=dependencies, trigger=trigger, owner=owner, deadline=deadline,
        cancelled=cancelled, delivery=delivery, critic_feedback=child.feedback)


def _execute_context_compile_batch(queue, lease, context, batch, *, coordinator, dependencies,
                                   trigger, owner, deadline, cancelled, delivery, critic_feedback):
    from compile_memory import _require_compile_active

    compiler_deadline = float('inf') if deadline is None else deadline
    _require_compile_active(compiler_deadline, cancelled)
    publication = _resolve_compile_publication(queue, lease, context, batch,
        coordinator=coordinator, dependencies=dependencies, delivery=delivery, deadline=deadline,
        critic_feedback=critic_feedback)
    _require_compile_active(compiler_deadline, cancelled)
    return execute_compile_publication(queue, lease, publication, batch, coordinator=coordinator,
        dependencies=dependencies, trigger=trigger, owner=owner, deadline=deadline, cancelled=cancelled)


def _check_batch_context_owner(queue, lease, saved, dependencies, deadline):
    dependencies.verify()
    operational_deadline_active(deadline)
    queue.verify_compile_checkpoint_owner(lease, saved)


def _publication_content(resolved):
    return {'plan': resolved.plan, 'request_action_key': resolved.action_key,
            'action_key': _publication_action_key(resolved.action_key, resolved.plan),
            'provider_budget': dict(resolved.provider_budget)}


def _publication_action_key(request_action_key, plan):
    progress._require_digest(request_action_key)
    return _compile_identity_digest({'schema': 'compile-publication-action/v1',
                                     'request_action_key': request_action_key, 'plan': plan})


def _require_publication_action(content):
    fields = {'plan', 'request_action_key', 'action_key', 'provider_budget'}
    if not isinstance(content, dict) or set(content) != fields:
        raise progress.ProgressCorrupt('invalid publication content fields')
    if content['action_key'] != _publication_action_key(content['request_action_key'], content['plan']):
        raise progress.ProgressCorrupt('publication action does not bind its accepted plan')


def _publication_binding(context, batch, content, dependencies):
    return {'schema': 'compile-publication/v2',
            'context_sequence': context.sequence,
            'context_digest': progress._event_digest(context),
            'batch_digest': _compile_identity_digest(_preflight_batch_details(batch)),
            'content_digest': _compile_identity_digest(content),
            'dependency_digest': dependencies.digest}


def _require_publication_context(queue, lease, context, batch, dependencies):
    _require_checkpoint(context)
    queue.verify_compile_checkpoint_owner(lease, context)
    recorded = queue.get_compile_event(context.work_id, sequence=context.sequence)
    if recorded != context or 'compile_context' not in context.details:
        raise progress.ProgressConflict('publication requires recorded batch context')
    sources = _compile_identity_digest(_preflight_source_records(batch.inputs))
    if sources != context.source_digest:
        raise progress.ProgressConflict('publication daily source identity changed')
    admission = queue.get_compile_event(context.work_id, sequence=0)
    _require_publication_dependencies(admission, dependencies)


def _require_publication_dependencies(admission, dependencies):
    if dependencies.verify() != admission.details['compile_batch']['dependency_digest']:
        raise progress.ProgressConflict('publication dependency identity changed')


def _publication_checkpoint(queue, lease, context, binding):
    latest = _compile_work_cursor(queue, context.work_id)
    queue.verify_compile_checkpoint_owner(lease, latest)
    if latest.stage == 'publication_ready':
        return _existing_publication_checkpoint(latest, binding)
    _require_checkpoint(latest)
    if latest.details['artifact'] != context.details['artifact']:
        raise progress.ProgressConflict('publication cursor lost its context artifact')
    return replace(context, task_id=lease.id, sequence=latest.sequence + 1,
                   stage='publication_ready', details={'publication': binding})


def _compile_work_cursor(queue, work_id):
    latest = queue.get_compile_event(work_id)
    if latest is None:
        raise progress.ProgressConflict('compile work is not recorded')
    if latest.stage != 'published':
        return latest
    return _completed_compile_parent(queue, latest)


def _completed_compile_parent(queue, latest):
    parent = queue.get_compile_event(latest.work_id, sequence=latest.sequence - 1)
    if parent is None or _completion_parent_digest(latest) != progress._event_digest(parent):
        raise progress.ProgressCorrupt('completed compile parent identity changed')
    return parent


def _completion_parent_digest(event):
    fields = {'publication_completion': 'publication_digest', 'repair_completion': 'repair_digest',
              'work_completion': 'preflight_digest'}
    if len(event.details) != 1:
        raise progress.ProgressCorrupt('invalid compile completion fields')
    kind = next(iter(event.details))
    try:
        return event.details[kind][fields[kind]]
    except (KeyError, TypeError) as error:
        raise progress.ProgressCorrupt('invalid compile completion parent') from error


def _existing_publication_checkpoint(latest, binding):
    if latest.outcome != 'ready' or latest.details.get('publication') != binding:
        raise progress.ProgressConflict('publication checkpoint identity changed')
    details = dict(latest.details)
    details.pop('artifact', None)
    return replace(latest, details=details)


def save_compile_publication(queue, lease, context, batch, resolved, *, dependencies, deadline=None):
    """Retain an accepted plan before Markdown writes; this is not a commit receipt."""
    from compile_memory import _require_apply_arguments

    operational_deadline_active(deadline)
    _require_publication_context(queue, lease, context, batch, dependencies)
    _require_apply_arguments(resolved.plan, batch.inputs, resolved.action_key, batch, resolved.provider_budget)
    content = json.loads(_snapshot_json(_publication_content(resolved)))
    binding = _publication_binding(context, batch, content, dependencies)
    event = _publication_checkpoint(queue, lease, context, binding)
    produce = partial(_write_publication_document, event, content, dependencies, deadline)
    validate = partial(_validate_publication_document, event, dependencies, deadline)
    saved = _save_verified_compile_artifact(queue, lease, event, produce, validate, deadline)
    _check_batch_context_owner(queue, lease, saved, dependencies, deadline)
    return saved


def _write_publication_document(event, content, dependencies, deadline, output):
    from compile_memory import _utc_now

    dependencies.verify()
    operational_deadline_active(deadline)
    document = {'schema': 'compile-publication-document/v2',
                'binding': event.details['publication'], 'content': content, 'completed_at': _utc_now()}
    output.write(_snapshot_json(document))
    dependencies.verify()
    operational_deadline_active(deadline)


def _require_publication_time(value):
    if not isinstance(value, str):
        raise progress.ProgressCorrupt('invalid publication timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.isoformat().replace('+00:00', 'Z') != value or not value.endswith('Z'):
        raise progress.ProgressCorrupt('invalid publication timestamp')


def _decoded_publication_document(event, data):
    document = json.loads(data)
    fields = {'schema', 'binding', 'content', 'completed_at'}
    if not isinstance(document, dict) or set(document) != fields:
        raise progress.ProgressCorrupt('invalid publication document fields')
    _require_publication_document_binding(event, document)
    _require_publication_action(document['content'])
    if _snapshot_json(document) != data:
        raise progress.ProgressCorrupt('noncanonical publication document')
    _require_publication_time(document['completed_at'])
    return document


def _require_publication_document_binding(event, document):
    expected = event.details['publication']
    if (document['schema'], document['binding']) != ('compile-publication-document/v2', expected):
        raise progress.ProgressCorrupt('publication document identity changed')
    if _compile_identity_digest(document['content']) != expected['content_digest']:
        raise progress.ProgressCorrupt('publication content identity changed')


def _validate_publication_document(event, dependencies, deadline, handle):
    _decoded_publication_document(event, handle.read())
    dependencies.verify()
    operational_deadline_active(deadline)


def read_compile_publication(queue, lease, event, *, deadline=None):
    """Read a verified intent; callers still require source/transaction receipts."""
    queue.verify_compile_checkpoint_owner(lease, event)
    recorded = queue.get_compile_event(event.work_id, sequence=event.sequence)
    if recorded != event or (event.stage, event.outcome) != ('publication_ready', 'ready'):
        raise progress.ProgressConflict('publication requires a recorded publication checkpoint')
    with queue.open_compile_artifact(event, deadline=deadline) as handle:
        document = _decoded_publication_document(event, handle.read())
    queue.verify_compile_checkpoint_owner(lease, event)
    operational_deadline_active(deadline)
    return {**document['content'], 'completed_at': document['completed_at']}


def _require_publication_completion_inputs(queue, lease, publication, batch, document, dependencies):
    binding = publication.details['publication']
    context = queue.get_compile_event(publication.work_id, sequence=binding['context_sequence'])
    _require_publication_context(queue, lease, context, batch, dependencies)
    content = {key: document[key] for key in ('plan', 'request_action_key', 'action_key', 'provider_budget')}
    if _publication_binding(context, batch, content, dependencies) != binding:
        raise progress.ProgressConflict('publication completion input identity changed')


def _require_publication_coordinator(queue, coordinator):
    from compile_memory import ROOT

    expected = (queue.vault, queue.vault, queue.state_root, queue.coordinator_path)
    actual = (ROOT, coordinator.vault, coordinator.state_root, coordinator.database_path)
    if None in expected:
        raise progress.ProgressConflict('publication coordinator identity requires an adopted queue')
    if tuple(Path(path).resolve(strict=True) for path in expected) != tuple(
            Path(path).resolve(strict=True) for path in actual):
        raise progress.ProgressConflict('publication coordinator identity differs from queue')


def confirm_compile_publication(queue, lease, publication, batch, *, coordinator, dependencies,
                                owner=None, deadline=None, cancelled=None):
    """Journal source completion only after receipt/transaction proof under the writer gate.

    None means no complete publication proof; it never finishes the whole queue task.
    A prior completion is revalidated, rather than used as a deletion permit.
    """
    from compile_memory import _compile_writer_gate

    _require_publication_coordinator(queue, coordinator)
    compiler_deadline = float('inf') if deadline is None else deadline
    with _compile_writer_gate(coordinator, owner, compiler_deadline, cancelled):
        return _confirm_publication_under_gate(queue, lease, publication, batch,
                                                coordinator, dependencies, deadline, cancelled)


def _confirm_publication_under_gate(queue, lease, publication, batch, coordinator, dependencies, deadline, cancelled):
    from compile_memory import compile_batch_completion

    document = read_compile_publication(queue, lease, publication, deadline=deadline)
    _require_publication_completion_inputs(queue, lease, publication, batch, document, dependencies)
    compiler_deadline = float('inf') if deadline is None else deadline
    result = compile_batch_completion(batch, coordinator, deadline=compiler_deadline, cancelled=cancelled)
    _check_batch_context_owner(queue, lease, publication, dependencies, deadline)
    return _record_publication_completion(queue, lease, publication, document, result)


def execute_compile_publication(queue, lease, publication, batch, *, coordinator, dependencies,
                                trigger, owner=None, deadline=None, cancelled=None):
    """Publish the recorded intent, recovering committed receipts before any write.

    Returns the verified completion event or None when publication has not produced
    complete source receipts. Neither outcome finishes the enclosing queue task.
    """
    from compile_memory import apply_compile_plan

    confirm = partial(confirm_compile_publication, queue, lease, publication, batch,
                      coordinator=coordinator, dependencies=dependencies, owner=owner,
                      deadline=deadline, cancelled=cancelled)
    completed = confirm()
    if completed is not None:
        return completed
    document = read_compile_publication(queue, lease, publication, deadline=deadline)
    active = partial(_publication_cancelled, queue, lease, publication, dependencies, deadline, cancelled)
    apply_compile_plan(batch.inputs, document['plan'], action_key=document['action_key'],
                       trigger=trigger, coordinator=coordinator, batch=batch,
                       provider_budget=document['provider_budget'], completed_at=document['completed_at'],
                       owner=owner, deadline=float('inf') if deadline is None else deadline, cancelled=active)
    return confirm()


def _publication_cancelled(queue, lease, publication, dependencies, deadline, cancelled):
    _check_batch_context_owner(queue, lease, publication, dependencies, deadline)
    return cancelled is not None and cancelled()


def _publication_completion_event(lease, publication, result):
    proof = {'schema': 'compile-publication-completion/v1',
             'publication_digest': progress._event_digest(publication),
             'transaction_id': result.transaction_id, 'operation_id': result.operation_id,
             'commit_sequence': result.commit_sequence, 'committed_at': result.committed_at,
             'action_key': result.action_key}
    return replace(publication, task_id=lease.id, sequence=publication.sequence + 1,
                   stage='published', outcome='completed', details={'publication_completion': proof})


def _record_publication_completion(queue, lease, publication, document, result):
    previous = queue.get_compile_event(publication.work_id, sequence=publication.sequence + 1)
    if result is None:
        return _missing_publication_receipts(previous)
    if result.action_key != document['action_key']:
        raise progress.ProgressConflict('receipts belong to another publication action')
    expected = _publication_completion_event(lease, publication, result)
    return _append_or_match_compile_completion(queue, lease, previous, expected)


def _missing_publication_receipts(previous):
    if previous is not None:
        raise progress.ProgressCorrupt('recorded publication completion has no complete source receipts')
    return None


def _append_or_match_compile_completion(queue, lease, previous, expected):
    if previous is None:
        queue.record_compile_event(lease, expected)
        return expected
    queue.verify_compile_checkpoint_owner(lease, previous)
    if replace(previous, task_id=lease.id) != expected:
        raise progress.ProgressConflict('compile completion authority changed')
    return previous


def _read_repair_child_context(queue, lease, child, parent_context, dependencies, deadline):
    admitted = queue.get_compile_event(child.work_id, sequence=0)
    if admitted is None:
        return None
    _resume_batch_admission(queue, lease, admitted, _repair_child_admission(lease, child, dependencies))
    saved = queue.get_compile_event(child.work_id, sequence=1)
    if saved is None:
        return None
    expected = _repair_child_context(lease, admitted, parent_context)
    _matched_compile_checkpoint(queue, lease, saved, expected)
    queue.verify_compile_artifact(saved, deadline=deadline)
    return saved


@dataclass(frozen=True)
class _RepairProofFrame:
    repair: progress.CompileEvent
    context: progress.CompileEvent
    batch: object
    children: tuple | None = None


@dataclass
class _RepairProof:
    queue: object
    lease: object
    coordinator: object
    dependencies: object
    deadline: float | None
    cancelled: object
    results: dict = dataclass_field(default_factory=dict)

    def run(self, repair, context, batch):
        stack = [_RepairProofFrame(repair, context, batch)]
        while stack:
            self._check_active()
            frame = stack.pop()
            if frame.children is None:
                self._expand(frame, stack)
                continue
            self._finish(frame)
        return self.results[repair.work_id]

    def _expand(self, frame, stack):
        children = read_compile_repair(self.queue, self.lease, frame.repair, frame.context, frame.batch,
                                       dependencies=self.dependencies, deadline=self.deadline)
        stack.append(replace(frame, children=children))
        for child in children:
            self._visit_child(frame, child, stack)

    def _visit_child(self, parent, child, stack):
        context = _read_repair_child_context(self.queue, self.lease, child, parent.context,
                                             self.dependencies, self.deadline)
        if context is None:
            self.results[child.work_id] = None
            return
        cursor = _compile_work_cursor(self.queue, child.work_id)
        if 'compile_repair' in cursor.details:
            stack.append(_RepairProofFrame(cursor, context, child.batch))
            return
        self.results[child.work_id] = self._leaf(cursor, child.batch)

    def _leaf(self, cursor, batch):
        if cursor.stage != 'publication_ready':
            return None
        return _confirm_publication_under_gate(self.queue, self.lease, cursor, batch,
            self.coordinator, self.dependencies, self.deadline, self.cancelled)

    def _finish(self, frame):
        self._check_active()
        _check_batch_context_owner(self.queue, self.lease, frame.repair, self.dependencies, self.deadline)
        children = tuple(self.results[child.work_id] for child in frame.children)
        self.results[frame.repair.work_id] = _record_repair_completion(self.queue, self.lease, frame.repair, children)

    def _check_active(self):
        from compile_memory import _require_compile_active

        _require_compile_active(float('inf') if self.deadline is None else self.deadline, self.cancelled)


def _record_repair_completion(queue, lease, repair, children):
    previous = queue.get_compile_event(repair.work_id, sequence=repair.sequence + 1)
    if not children or any(child is None for child in children):
        return _missing_repair_completion(previous)
    expected = _repair_completion_event(lease, repair, children)
    return _append_or_match_compile_completion(queue, lease, previous, expected)


def _missing_repair_completion(previous):
    if previous is not None:
        raise progress.ProgressCorrupt('recorded repair completion has incomplete source receipts')
    return None


def _repair_completion_event(lease, repair, children):
    proof = {'schema': 'compile-repair-completion/v1', 'repair_digest': progress._event_digest(repair),
             'children': [{'work_id': child.work_id, 'sequence': child.sequence,
                           'event_digest': progress._event_digest(child)} for child in children]}
    return replace(repair, task_id=lease.id, sequence=repair.sequence + 1,
                   stage='published', outcome='completed', details={'repair_completion': proof})


def confirm_compile_repair(queue, lease, repair, context, batch, *, coordinator, dependencies,
                           owner=None, deadline=None, cancelled=None):
    """Prove every descendant receipt in one cooperating-writer snapshot; never run work."""
    from compile_memory import _compile_writer_gate

    _require_publication_coordinator(queue, coordinator)
    compiler_deadline = float('inf') if deadline is None else deadline
    with _compile_writer_gate(coordinator, owner, compiler_deadline, cancelled):
        return _RepairProof(queue, lease, coordinator, dependencies, deadline, cancelled).run(repair, context, batch)


def execute_compile_work(queue, lease, snapshot, *, coordinator, dependencies, request, plan,
                         trigger, owner=None, deadline=None, cancelled=None,
                         capture=_capture_current_compile_context):
    """Execute every retained root, then prove full coverage before returning success.

    Preflight refusals remain in the retained plan and prevent final proof. The
    worker parent owns heartbeat and queue settlement. Unknown calls block this
    delivery after independent roots; they never become successful work.
    """
    from compile_memory import _require_compile_active

    _require_publication_coordinator(queue, coordinator)
    _require_compile_active(float('inf') if deadline is None else deadline, cancelled)
    items = iter_compile_preflight(queue, lease, snapshot, dependencies=dependencies,
        request=request, plan=plan, on_refused=None, deadline=deadline)
    with CompileDelivery(queue, lease) as delivery, closing(items):
        for planned in items:
            execute_planned_compile_tree(queue, lease, planned, snapshot, coordinator=coordinator,
                dependencies=dependencies, trigger=trigger, owner=owner, deadline=deadline,
                cancelled=cancelled, delivery=delivery, capture=capture)
        return confirm_compile_work(queue, lease, snapshot, coordinator=coordinator,
            dependencies=dependencies, request=request, plan=plan, owner=owner,
            deadline=deadline, cancelled=cancelled)


def confirm_compile_work(queue, lease, snapshot, *, coordinator, dependencies, request, plan,
                         owner=None, deadline=None, cancelled=None):
    """Prove every root and refusal in one writer snapshot, without creating child work."""
    from compile_memory import _compile_writer_gate, _require_compile_active

    _require_publication_coordinator(queue, coordinator)
    compiler_deadline = float('inf') if deadline is None else deadline
    _require_compile_active(compiler_deadline, cancelled)
    preflight = save_compile_preflight(queue, lease, snapshot, dependencies=dependencies,
        request=request, plan=plan, deadline=deadline)
    with _compile_writer_gate(coordinator, owner, compiler_deadline, cancelled):
        return _CompileWorkProof(queue, lease, snapshot, coordinator, dependencies,
                                 deadline, cancelled).run(preflight, plan)


@dataclass
class _CompileWorkProof:
    queue: object
    lease: object
    snapshot: progress.CompileEvent
    coordinator: object
    dependencies: object
    deadline: float | None
    cancelled: object
    batch_count: int = 0
    incomplete: bool = False
    digest: object = dataclass_field(default_factory=hashlib.sha256)

    def run(self, preflight, plan):
        items = _iter_saved_compile_preflight(self.queue, self.lease, self.snapshot, preflight,
            self.dependencies, plan, self.refused, self.deadline)
        with closing(items):
            for planned in items:
                self._check_active()
                self._batch(planned)
        self._check_active()
        _check_preflight_consumer(self.queue, self.lease, preflight, self.dependencies, self.deadline)
        return self._record(preflight)

    def refused(self, _inputs):
        self._check_active()
        self.incomplete = True

    def _check_active(self):
        from compile_memory import _require_compile_active

        _require_compile_active(float('inf') if self.deadline is None else self.deadline, self.cancelled)

    def _batch(self, planned):
        self.batch_count += 1
        result = self._confirm_planned(planned)
        if result is None:
            self.incomplete = True
            return
        record = {'work_id': result.work_id, 'sequence': result.sequence,
                  'event_digest': progress._event_digest(result)}
        self.digest.update(_snapshot_json(record) + b'\n')

    def _confirm_planned(self, planned):
        from compile_memory import refresh_compile_batch_from_context

        saved = self.queue.get_compile_event(planned.work_id, sequence=1)
        if saved is None:
            return None
        with open_compile_batch_context(self.queue, self.lease, planned, self.snapshot, saved,
                dependencies=self.dependencies, deadline=self.deadline) as context:
            batch = refresh_compile_batch_from_context(planned.batch, context,
                deadline=float('inf') if self.deadline is None else self.deadline)
            return self._confirm_tree(saved, batch)

    def _confirm_tree(self, context, batch):
        cursor = _compile_work_cursor(self.queue, context.work_id)
        if 'compile_repair' in cursor.details:
            return _RepairProof(self.queue, self.lease, self.coordinator, self.dependencies,
                                self.deadline, self.cancelled).run(cursor, context, batch)
        if cursor.stage == 'publication_ready':
            return _confirm_publication_under_gate(self.queue, self.lease, cursor, batch,
                self.coordinator, self.dependencies, self.deadline, self.cancelled)
        return None

    def _record(self, preflight):
        previous = self.queue.get_compile_event(preflight.work_id, sequence=preflight.sequence + 1)
        if self.incomplete:
            return _missing_work_completion(previous)
        proof = {'schema': 'compile-work-completion/v1', 'preflight_digest': progress._event_digest(preflight),
                 'batch_count': self.batch_count, 'children_digest': self.digest.hexdigest()}
        expected = replace(preflight, task_id=self.lease.id, sequence=preflight.sequence + 1,
                           stage='published', outcome='completed', details={'work_completion': proof})
        return _append_or_match_compile_completion(self.queue, self.lease, previous, expected)


def _missing_work_completion(previous):
    if previous is not None:
        raise progress.ProgressCorrupt('recorded work completion has incomplete source receipts')
    return None


def load_compile_inputs(queue, lease, checkpoint):
    """Explicit eager compatibility for callers that require a complete value."""
    with open_compile_inputs(queue, lease, checkpoint) as snapshot:
        return snapshot.materialize()


def _require_input_checkpoint(checkpoint):
    if (checkpoint.stage, checkpoint.outcome) != ('snapshot_ready', 'ready'):
        raise progress.ProgressConflict('captured inputs require a ready snapshot checkpoint')
    if 'model_call' in checkpoint.details:
        raise progress.ProgressConflict('model answer is not an input snapshot')


_ARCHIVE_SCHEMA = 'compile-input-archive/v1'
_ADMITTED_ARCHIVE_SCHEMA = 'compile-input-archive/v2'
_ARCHIVE_BYTES_FIELDS = ('content', 'original_content', 'prompt_content')
_ARCHIVE_JSON_FIELDS = ('original_entries', 'native_frames')


def _snapshot_json(value):
    return progress._canonical(value).encode('utf-8')


def _blob_digest(content):
    if content is None:
        return None
    if type(content) is not bytes:
        raise ValueError('snapshot content must be bytes')
    return hashlib.sha256(content).hexdigest()


def _archive_record_value(record, put):
    _require_snapshot_digest(record.content, record.sha256)
    value = asdict(record)
    _archive_record_fields(value, _ARCHIVE_BYTES_FIELDS, put)
    _require_original_digest(value)
    _archive_record_fields(value, _ARCHIVE_JSON_FIELDS, partial(_put_snapshot_json, put))
    return value


def _archive_record_fields(value, names, put):
    for name in names:
        if name in value:
            value[name] = put(value[name])


def _put_snapshot_json(put, value):
    return put(_snapshot_json(value))


def _archive_manifest(inputs, put, write_record):
    records = {group: _write_record_metadata(group, _metadata_rows(getattr(inputs, group), put), write_record)
               for group in _snapshot_types()}
    return {'schema': _ARCHIVE_SCHEMA, 'records': records}


def _metadata_rows(records, put):
    for record in records:
        yield _record_metadata_row(record, put)


def _record_metadata_row(record, put):
    return record.logical_path, _snapshot_json(_archive_record_value(record, put))


def _record_manifest(records, put, write_record, *, sort_groups=()):
    metadata = {group: [] for group in _snapshot_types()}
    for group, record in records:
        metadata[group].append(_record_metadata_row(record, put))
    for group in sort_groups:
        metadata[group].sort(key=lambda entry: entry[0])
    digests = {group: _write_record_metadata(group, rows, write_record)
               for group, rows in metadata.items()}
    return {'schema': _ARCHIVE_SCHEMA, 'records': digests}


def _write_record_metadata(group, rows, write_record):
    digests = []
    for index, (_, data) in enumerate(rows):
        digests.append(hashlib.sha256(data).hexdigest())
        write_record(_archive_record_name(group, index), data)
    return digests


def _archive_record_name(group, index):
    return f'records/{group}/{index}.json'


def _discard_record(_name, _data):
    """Identity computation does not publish archive entries."""


def compile_inputs_digest(inputs):
    """Bind all record metadata and original byte digests, without base64 copies."""
    manifest = _archive_manifest(inputs, _blob_digest, _discard_record)
    return hashlib.sha256(_snapshot_json(manifest)).hexdigest()


class _SnapshotArchiveWriter:
    def __init__(self, archive):
        self.archive = archive
        self.blobs = set()

    def put(self, content):
        digest = _blob_digest(content)
        if digest is None or digest in self.blobs:
            return digest
        self.write('blobs/' + digest, content)
        self.blobs.add(digest)
        return digest

    def write(self, name, data):
        # A fixed ZIP epoch makes retries byte-identical; it is not source time.
        info = zipfile.ZipInfo(name)
        info.compress_type = zipfile.ZIP_STORED
        self.archive.writestr(info, data)


def _write_archive_snapshot(inputs, expected, output):
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        writer = _SnapshotArchiveWriter(archive)
        manifest = _archive_manifest(inputs, writer.put, writer.write)
        data = _snapshot_json(manifest)
        _require_snapshot_digest(data, expected)
        writer.write('manifest.json', data)


_CONTEXT_GROUPS = ('sources', 'targets', 'vault_files')
_CONTEXT_DELTA_SCHEMA = 'compile-context-delta/v2'


def _require_complete_context_base(base):
    if base.manifest['schema'] not in {_ARCHIVE_SCHEMA, _ADMITTED_ARCHIVE_SCHEMA}:
        raise progress.ProgressConflict('context delta requires a complete base snapshot')


def _context_record_index(reader, group):
    indexed = {}
    for index in range(reader.record_count(group)):
        path = reader.record_metadata(group, index)['logical_path']
        _index_context_path(indexed, path, index)
    return indexed


def _index_context_path(indexed, path, index):
    if path in indexed:
        raise progress.ProgressCorrupt('duplicate context record path')
    indexed[path] = index


def _context_record_digests(reader, group):
    indexed = _context_record_index(reader, group)
    return {path: reader.manifest['records'][group][index] for path, index in indexed.items()}


def _require_context_record_type(group, record):
    if type(record) is not _snapshot_types()[group]:
        raise ValueError('invalid context record type for group')


class _ContextDeltaWriter:
    def __init__(self, base, archive, binding):
        self.binding = binding
        self.base_digest = base.digest
        self.previous = {group: _context_record_digests(base, group) for group in _CONTEXT_GROUPS}
        self.seen = {group: set() for group in _CONTEXT_GROUPS}
        self.rows = {group: [] for group in _snapshot_types()}
        self.writer = _SnapshotArchiveWriter(archive)

    def capture(self, group, record):
        old = self._consume_path(group, record.logical_path)
        _require_context_record_type(group, record)
        _require_snapshot_digest(record.content, record.sha256)
        path, data = _record_metadata_row(record, _blob_digest)
        if _blob_digest(data) == old:
            return
        self.rows[group].append(_record_metadata_row(record, self.writer.put))

    def _consume_path(self, group, path):
        if group not in self.previous:
            raise ValueError('invalid context delta record group')
        if path in self.seen[group]:
            raise ValueError('duplicate current context record path')
        self.seen[group].add(path)
        return self.previous[group].pop(path, None)

    def finish(self):
        records = {group: _write_record_metadata(group, sorted(rows), self.writer.write)
                   for group, rows in self.rows.items()}
        removed = {group: sorted(paths) for group, paths in self.previous.items()}
        manifest = {'schema': _CONTEXT_DELTA_SCHEMA, 'base_digest': self.base_digest,
                    'records': records, 'removed': removed, 'binding': self.binding}
        data = _snapshot_json(manifest)
        self.writer.write('manifest.json', data)
        return _blob_digest(data)


def write_compile_context_delta(output, base, records, *, binding=None, deadline=None):
    """Write self-contained changed context records against the complete capture.

    Unchanged bodies stay in that capture. No blob in this archive references
    another archive, and no chain of context deltas is followed. The existing
    queue publication boundary must own both references before production use.
    """
    _require_complete_context_base(base)
    _require_context_binding(binding)
    operational_deadline_active(deadline)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        writer = _ContextDeltaWriter(base, archive, binding)
        for group, record in records:
            operational_deadline_active(deadline)
            writer.capture(group, record)
        result = writer.finish()
    operational_deadline_active(deadline)
    return result


def _decoded_context_delta(data):
    fields = {'schema', 'base_digest', 'records', 'removed', 'binding'}
    value = _decoded_snapshot_manifest(data, _CONTEXT_DELTA_SCHEMA, fields)
    progress._require_digest(value['base_digest'])
    _require_context_binding(value['binding'])
    _require_removed_context(value['removed'])
    if value['records']['dailies']:
        raise progress.ProgressCorrupt('context delta contains daily records')
    return value


def _require_context_binding(binding):
    if binding is not None:
        progress._require_digest(binding)


def _require_removed_context(removed):
    if not isinstance(removed, dict) or set(removed) != set(_CONTEXT_GROUPS):
        raise progress.ProgressCorrupt('invalid removed context groups')
    for paths in removed.values():
        _require_removed_context_paths(paths)


def _require_removed_context_paths(paths):
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        raise progress.ProgressCorrupt('invalid removed context paths')
    if sorted(set(paths)) != paths:
        raise progress.ProgressCorrupt('removed context paths must be unique and sorted')


def read_compile_context_delta(archive, expected, base, *, binding=None):
    """Return a lazy complete context while both caller-owned archives stay open."""
    from compile_memory import CompileInputs

    _require_complete_context_base(base)
    delta = CompileInputArchive(archive, expected, manifest_decoder=_decoded_context_delta)
    if (delta.manifest['base_digest'], delta.manifest['binding']) != (base.digest, binding):
        raise progress.ProgressConflict('context delta base identity changed')
    groups = {group: _overlay_context_records(base, delta, group) for group in _CONTEXT_GROUPS}
    return CompileInputs((), **groups)


def _require_context_removals(base, changed, removed):
    if not removed.issubset(base):
        raise progress.ProgressCorrupt('removed context path is absent from base')
    if removed.intersection(changed):
        raise progress.ProgressCorrupt('context path is both changed and removed')


def _overlay_context_records(base, delta, group):
    original = _context_record_index(base, group)
    changed = _context_record_index(delta, group)
    removed = set(delta.manifest['removed'][group])
    _require_context_removals(original, changed, removed)
    records = {path: (base, index) for path, index in original.items() if path not in removed}
    records.update({path: (delta, index) for path, index in changed.items()})
    paths = tuple(sorted(records))
    return _ContextRecords(group, tuple(records[path] for path in paths), paths)


class _ContextRecords(Sequence):
    def __init__(self, group, records, paths):
        self.group, self.records, self.paths = group, records, paths

    def __len__(self):
        return len(self.records)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return _ContextRecords(self.group, self.records[index], self.paths[index])
        reader, position = self.records[index]
        return reader.record(self.group, position)

    def logical_paths(self):
        return self.paths

    def select_paths(self, wanted):
        positions = tuple(index for index, path in enumerate(self.paths) if path in wanted)
        return _ContextRecords(self.group, tuple(self.records[index] for index in positions),
                               tuple(self.paths[index] for index in positions))


def compile_capture_digest(paths, *, compiled=None, deadline=None):
    """Compute the source identity without retaining captured source bodies."""
    operational_deadline_active(deadline)
    put = partial(_deadline_blob_digest, deadline)
    manifest = _capture_manifest(paths, compiled, put, _discard_record)
    result = hashlib.sha256(_snapshot_json(manifest)).hexdigest()
    operational_deadline_active(deadline)
    return result


def _deadline_blob_digest(deadline, content):
    operational_deadline_active(deadline)
    return _blob_digest(content)


def _capture_manifest(paths, compiled, put, write):
    from compile_memory import iter_compile_records

    return _record_manifest(iter_compile_records(paths, compiled=compiled), put, write,
                            sort_groups=('sources', 'targets'))


def write_compile_capture(output, paths, *, compiled=None):
    """Stream source capture; return identity only after a complete container.

    The caller owns durable staging and queue/work admission. Partial output
    after an exception must never be published as a ready snapshot.
    """
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        writer = _SnapshotArchiveWriter(archive)
        manifest = _capture_manifest(paths, compiled, writer.put, writer.write)
        data = _snapshot_json(manifest)
        writer.write('manifest.json', data)
    return hashlib.sha256(data).hexdigest()


class CompileInputArchive:
    """Context-owned record reader; opening keeps metadata, not source bodies."""

    def __init__(self, archive, expected, *, manifest_decoder=None):
        self.archive = archive
        _require_archive_members(archive)
        data = archive.read('manifest.json')
        _require_snapshot_digest(data, expected)
        decoder = manifest_decoder or _decoded_archive_manifest
        self.manifest = decoder(data)
        _require_archive_records(archive, self.manifest)
        self.digest = expected

    def record_count(self, group):
        return len(self.manifest['records'][group])

    def record_metadata(self, group, index):
        expected = self.manifest['records'][group][index]
        data = self.archive.read(_archive_record_name(group, index))
        _require_snapshot_digest(data, expected)
        value = json.loads(data)
        if _snapshot_json(value) != data:
            raise progress.ProgressCorrupt('noncanonical snapshot record')
        return value

    def blob(self, digest):
        if digest is None:
            return None
        progress._require_digest(digest)
        content = self.archive.read('blobs/' + digest)
        _require_snapshot_digest(content, digest)
        return content

    def verify_blob(self, digest, *, deadline=None):
        """Validate every byte through EOF without retaining the blob body."""
        progress._require_digest(digest)
        operational_deadline_active(deadline)
        with self.archive.open('blobs/' + digest) as stream:
            actual = _stream_snapshot_digest(stream, deadline)
        if actual != digest:
            raise ValueError('snapshot content digest mismatch')
        operational_deadline_active(deadline)

    @contextmanager
    def original_range(self, digest, *, deadline=None):
        progress._require_digest(digest)
        operational_deadline_active(deadline)
        name = 'blobs/' + digest
        size = self.archive.getinfo(name).file_size
        with self.archive.open(name) as stream:
            yield VerifiedByteRange(stream, size, digest, deadline, chunk_size=IO_CHUNK_BYTES)

    def json_blob(self, digest):
        data = self.blob(digest)
        value = json.loads(data)
        if _snapshot_json(value) != data:
            raise progress.ProgressCorrupt('noncanonical snapshot metadata')
        return value

    def record(self, group, index):
        return _restore_archive_value(group, self.record_metadata(group, index),
                                      self.blob, self.json_blob)

    def records(self, group):
        if group == 'dailies':
            yield from self.daily_records()
            return
        for index in range(self.record_count(group)):
            yield self.record(group, index)

    def daily_records(self):
        metadata = (self.record_metadata('dailies', index)
                    for index in range(self.record_count('dailies')))
        for _, records in groupby(metadata, key=lambda value: value['logical_path']):
            yield from _restore_archive_day(self, records)

    def iter_batches(self, *, model, token_adapters=None, budget=None,
                     planning_candidates=None, context_pending=False, on_refused=None):
        from compile_memory import iter_streamed_compile_batches

        required = self.required_context()
        yield from iter_streamed_compile_batches(
            self.daily_records(), self.batch_context_inputs(), required,
            model=model, token_adapters=token_adapters, budget=budget,
            planning_candidates=planning_candidates, context_pending=context_pending,
            on_refused=on_refused,
        )

    def batch_context_inputs(self):
        from compile_memory import CompileInputs

        pending = {self.record_metadata('dailies', index)['logical_path']
                   for index in range(self.record_count('dailies'))}
        sources = ArchiveRecords(self, 'sources')
        context = sources.select_paths(set(sources.logical_paths()) - pending)
        return CompileInputs((), context, ArchiveRecords(self, 'targets'),
                             ArchiveRecords(self, 'vault_files'))

    def required_context(self):
        from compile_memory import CompileInputs, _RequiredCitationContext

        inputs = CompileInputs((), ArchiveRecords(self, 'sources'), ())
        return _RequiredCitationContext(inputs, None, daily_records=self.daily_records)

    def planning_inputs(self):
        from compile_memory import CompileInputs

        return CompileInputs(tuple(self.records('dailies')),
                             ArchiveRecords(self, 'sources'), ArchiveRecords(self, 'targets'),
                             ArchiveRecords(self, 'vault_files'))

    def materialize(self):
        from compile_memory import CompileInputs

        return CompileInputs(**{group: tuple(self.records(group)) for group in _snapshot_types()})


@dataclass(frozen=True, eq=False)
class _OriginalDigestProof:
    """A computed digest tied to a strongly held, exact immutable bytes object."""

    content: bytes | None = dataclass_field(repr=False)
    digest: str | None = dataclass_field(init=False)

    def __post_init__(self):
        object.__setattr__(self, 'digest', _blob_digest(self.content))


def _reuse_original_proof(previous, content):
    if type(previous) is _OriginalDigestProof and previous.content is content:
        return previous
    return _OriginalDigestProof(content)


def _proven_original_digest(content, proof):
    if proof is None:
        return _blob_digest(content)
    if type(proof) is not _OriginalDigestProof or proof.content is not content:
        raise ValueError('original digest proof does not own these bytes')
    return proof.digest


def _restore_archive_day(reader, records):
    originals, metadata = {}, {}
    proof = None
    for value in records:
        originals = _shared_record_blobs(value, ('original_content',), reader.blob, originals)
        metadata = _shared_record_blobs(value, _ARCHIVE_JSON_FIELDS, reader.json_blob, metadata)
        proof = _reuse_original_proof(proof, originals.get(value.get('original_content')))
        blob = partial(_read_shared_blob, reader.blob, originals)
        json_blob = partial(_read_shared_blob, reader.json_blob, metadata)
        yield _restore_archive_value('dailies', value, blob, json_blob, original_proof=proof)


def _shared_record_blobs(value, fields, read, previous):
    """Keep only identities the current record names as shared fields."""
    digests = {value[name] for name in fields if name in value}
    return {digest: _read_shared_blob(read, previous, digest) for digest in digests}


def _read_shared_blob(read, shared, digest):
    if digest in shared:
        return shared[digest]
    return read(digest)


def _restore_archive_value(group, value, blob, json_blob, *, original_proof=None):
    _archive_record_fields(value, _ARCHIVE_BYTES_FIELDS, blob)
    _archive_record_fields(value, _ARCHIVE_JSON_FIELDS, json_blob)
    return _restored_archive_record(_snapshot_types()[group], value, original_proof=original_proof)


def _restored_archive_record(kind, value, *, original_proof=None):
    from compile_memory import DailySnapshot

    if kind is DailySnapshot:
        digest = _proven_original_digest(value['original_content'], original_proof)
        _require_original_digest({**value, 'original_content': digest})
        value = _restore_snapshot_daily(value)
    result = kind(**value)
    _require_snapshot_digest(result.content, result.sha256)
    return result


def _decoded_archive_manifest(data):
    value = json.loads(data)
    if not isinstance(value, dict):
        raise progress.ProgressCorrupt('invalid snapshot archive manifest')
    if value.get('schema') == _ADMITTED_ARCHIVE_SCHEMA:
        decoded = _decoded_snapshot_manifest(data, _ADMITTED_ARCHIVE_SCHEMA, {'schema', 'records', 'admission'})
        _require_capture_admission(decoded['admission'])
        return decoded
    return _decoded_snapshot_manifest(data, _ARCHIVE_SCHEMA, {'schema', 'records'})


def _decoded_snapshot_manifest(data, schema, expected):
    value = json.loads(data)
    if not isinstance(value, dict) or set(value) != expected or value['schema'] != schema:
        raise progress.ProgressCorrupt('unknown snapshot archive schema')
    _require_archive_record_groups(value['records'])
    if _snapshot_json(value) != data:
        raise progress.ProgressCorrupt('invalid snapshot archive manifest')
    return value


def _require_archive_members(archive):
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise progress.ProgressCorrupt('duplicate snapshot archive member')
    for info in archive.infolist():
        _require_stored_member(info)


def _require_stored_member(info):
    if info.compress_type != zipfile.ZIP_STORED or info.flag_bits & 1:
        raise progress.ProgressCorrupt('snapshot members must be uncompressed and unencrypted')
    if info.file_size != info.compress_size:
        raise progress.ProgressCorrupt('snapshot member size mismatch')


def _require_archive_records(archive, manifest):
    expected = {'manifest.json'}
    for group, digests in manifest['records'].items():
        expected.update(_archive_group_names(group, digests))
    actual = set(archive.namelist())
    for name in actual - expected:
        _require_blob_member(name)
    if not expected.issubset(actual):
        raise progress.ProgressCorrupt('snapshot record is missing')


def _require_blob_member(name):
    if not name.startswith('blobs/'):
        raise progress.ProgressCorrupt('unknown snapshot member')
    progress._require_digest(name.removeprefix('blobs/'))


def _require_archive_record_groups(records):
    if not isinstance(records, dict) or set(records) != set(_snapshot_types()):
        raise progress.ProgressCorrupt('invalid snapshot record groups')


def _archive_group_names(group, digests):
    if not isinstance(digests, list):
        raise progress.ProgressCorrupt('snapshot record digests must be a list')
    for index, digest in enumerate(digests):
        progress._require_digest(digest)
        yield _archive_record_name(group, index)


class ArchiveRecords(Sequence):
    """Explicit file-backed sequence; every loaded record belongs to its reader."""

    def __init__(self, reader, group, indices=None, paths=None):
        self.reader, self.group = reader, group
        self.indices = tuple(range(reader.record_count(group))) if indices is None else indices
        self.paths = tuple(reader.record_metadata(group, index)['logical_path']
                           for index in self.indices) if paths is None else paths

    def __len__(self):
        return len(self.indices)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return ArchiveRecords(self.reader, self.group, self.indices[index], self.paths[index])
        return self.reader.record(self.group, self.indices[index])

    def logical_paths(self):
        return self.paths

    def select_paths(self, wanted):
        positions = tuple(index for index, path in enumerate(self.paths) if path in wanted)
        return ArchiveRecords(self.reader, self.group,
                              tuple(self.indices[index] for index in positions),
                              tuple(self.paths[index] for index in positions))


def _compile_identity_digest(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _compile_dependency_file(path, deadline):
    path = Path(path)
    resolved = path.resolve(strict=True)
    digest, identity = fingerprint_regular_file(resolved, resolved.parent, deadline=deadline)
    if path.resolve(strict=True) != resolved:
        raise PermissionError('compile dependency resolution changed during fingerprint')
    return {'sha256': digest, 'size': identity.size,
            'location_digest': _compile_identity_digest(str(resolved))}


def compile_dependency_manifest(files, parameters, *, deadline=None):
    """Fingerprint an explicitly inventoried dependency set, not infer its closure.

    Callers own inventory completeness and must rebuild it before accepting replay.
    Only hashes of effective configuration and resolved locations are persisted.
    No file-size ceiling is imposed; stable streaming reads obey caller deadlines.
    """
    operational_deadline_active(deadline)
    configuration = _compile_identity_digest(parameters)
    records = {name: _compile_dependency_file(path, deadline) for name, path in files.items()}
    manifest = {'schema': 'compile-dependencies/v1', 'files': records,
                'parameters_digest': configuration}
    compile_dependency_digest(manifest)
    return manifest


def compile_dependency_digest(manifest):
    """Bind an inventoried dependency manifest to canonical bytes."""
    return _compile_identity_digest(manifest)


def _compile_configuration_identity(root, deadline):
    import model_selection
    import settings
    from llm_client import _policy_identity, provider_environment
    from model_dlp import load_policy

    operational_deadline_active(deadline)
    environment = provider_environment()
    with model_selection.using_root(root):
        chosen = model_selection.current()
        values = {name: item.value for name, item in settings.effective(root, environment).items()}
        model = None if chosen is None else model_selection.selection_table(chosen)
        policy = _policy_identity(load_policy())
        raw_digest = hashlib.sha256(model_selection.file_bytes(root)).hexdigest()
    operational_deadline_active(deadline)
    return _compile_identity_digest({'root': str(Path(root).resolve(strict=True)),
        'settings_bytes': raw_digest, 'settings': values, 'model': model,
        'provider_environment': environment, 'dlp_policy': policy})


def _require_same_compile_configuration(before, after):
    if before != after:
        raise progress.ProgressConflict('compile configuration changed during inventory')


def compile_configuration_dependency_manifest(root, *, deadline=None):
    """Observe settings, scoped model, validated DLP and the actual child environment.

    Only the resulting digest escapes. Two equal observations reject detected
    concurrent changes; they do not lock noncooperating editors or prevent ABA.
    """
    before = _compile_configuration_identity(root, deadline)
    _require_same_compile_configuration(before, _compile_configuration_identity(root, deadline))
    return {'schema': 'compile-configuration/v1', 'parameters_digest': before}


def compile_execution_dependency_manifest(root, parameters, *, deadline=None):
    """Compose verified inventories without claiming loaded-code or OS closure."""
    configuration = compile_configuration_dependency_manifest(root, deadline=deadline)
    product = compile_product_dependency_manifest(root, parameters, deadline=deadline)
    packages = compile_package_dependency_manifest(deadline=deadline)
    runtime = compile_runtime_dependency_manifest(deadline=deadline)
    _require_same_compile_configuration(configuration,
        compile_configuration_dependency_manifest(root, deadline=deadline))
    return {'schema': 'compile-execution-dependencies/v1', 'configuration': configuration,
            'product': product, 'packages': packages, 'python': runtime}


def capture_compile_dependencies(root, parameters, *, deadline=None):
    """Build a live re-inspected witness from the same execution inventory."""
    inspect = partial(compile_execution_dependency_manifest, root, parameters, deadline=deadline)
    return CompileDependencies.capture(inspect)


def compile_work_identity(origin_task_id, source_digest, dependency_digest, request):
    """Bind execution to its original queue task, retained across explicit redrive."""
    if not isinstance(origin_task_id, str) or not origin_task_id:
        raise ValueError('compile origin task identity must be nonempty text')
    progress._require_digest(source_digest)
    progress._require_digest(dependency_digest)
    return progress.work_identity(origin_task_id, source_digest, dependency_digest,
                                      _compile_identity_digest(request))



def _regenerable_dependency_bytecode(path):
    from importlib.util import source_from_cache

    if path.suffix != '.pyc':
        return False
    try:
        return Path(source_from_cache(str(path))).is_file()
    except ValueError:
        return False


def _compile_dependency_tree(directory, ancestors=(), *, deadline=None, excluded=frozenset()):
    operational_deadline_active(deadline)
    resolved = directory.resolve(strict=True)
    if resolved in excluded:
        return
    _require_acyclic_dependency_directory(resolved, ancestors)
    for child in sorted(directory.iterdir()):
        yield from _compile_dependency_entry(child, (*ancestors, resolved), deadline, excluded)
    if directory.resolve(strict=True) != resolved:
        raise PermissionError('compile dependency directory resolution changed')


def _compile_dependency_entry(path, ancestors, deadline, excluded):
    if path.is_dir():
        yield from _compile_dependency_tree(path, ancestors, deadline=deadline, excluded=excluded)
        return
    if not _regenerable_dependency_bytecode(path):
        yield path


def compile_product_dependency_manifest(root, parameters, *, deadline=None):
    """Inventory compiler implementation/data and declared package requirements.

    This product component is not the interpreter or installed-package component.
    Generated bytecode is excluded only when its source still exists. Directory
    symlinks are supported, with cycles refused rather than silently truncated.
    Re-inventory is required at admission: this traversal is not a filesystem lock.
    """
    root = Path(root)
    files = {path.relative_to(root).as_posix(): path
             for path in _compile_dependency_tree(root / 'scripts', deadline=deadline)}
    files.update({name: root / name for name in ('pyproject.toml', 'uv.lock')})
    return compile_dependency_manifest(files, parameters, deadline=deadline)


def _compile_distribution_inventory(distribution):
    direct = json.loads(distribution.read_text('direct_url.json') or '{}')
    if direct.get('dir_info', {}).get('editable'):
        raise ValueError('editable package requires an external source inventory')
    files = distribution.files
    if files is None:
        raise ValueError('installed package has no verifiable file inventory')
    return files


def _compile_distribution_files(distribution, *, deadline=None):
    operational_deadline_active(deadline)
    entries = _compile_distribution_inventory(distribution)
    for entry in entries:
        path = Path(distribution.locate_file(entry))
        if not _regenerable_dependency_bytecode(path):
            yield str(entry), path


def compile_package_dependency_manifest(*, deadline=None):
    """Fingerprint actual declared package bytes, independent of RECORD digests.

    Filesystem distributions with complete inventories are supported here.
    Editable sources need their own inventory; absence is refused, never silently
    treated as a complete package. Interpreter/stdlib and loaded-code identity
    remain separate components of the execution environment.
    """
    import importlib.metadata
    import sys

    operational_deadline_active(deadline)
    files, packages = {}, {}
    for distribution in importlib.metadata.distributions():
        operational_deadline_active(deadline)
        location = str(Path(distribution.locate_file('')).resolve(strict=True))
        key = _compile_identity_digest([distribution.metadata['Name'], distribution.version, location])
        packages[key] = {'name': distribution.metadata['Name'], 'version': distribution.version}
        files.update({f'{key}/{name}': path for name, path in
                      _compile_distribution_files(distribution, deadline=deadline)})
    return compile_dependency_manifest(files, {'packages': packages, 'sys_path': sys.path},
                                       deadline=deadline)


@dataclass(frozen=True)
class CompileDependencies:
    """An expected inventory plus a live inspector, never just an unchecked hash.

    The workflow persists digest and reconstructs the inspector on recovery.
    Inventory completeness remains the inspector's contract. Rechecking cannot
    lock noncooperating installers; execution still requires the admission fence.
    """

    digest: str
    inspect: Callable[[], object]

    def __post_init__(self):
        progress._require_digest(self.digest)
        if not callable(self.inspect):
            raise TypeError('compile dependency inspector must be callable')

    @classmethod
    def capture(cls, inspect):
        return cls(compile_dependency_digest(inspect()), inspect)

    def verify(self):
        if compile_dependency_digest(self.inspect()) != self.digest:
            raise progress.ProgressConflict('compile dependency identity changed')
        return self.digest


def _require_acyclic_dependency_directory(resolved, ancestors):
    if resolved in ancestors:
        raise ValueError('compile dependency directory cycle')


def _compile_runtime_library(config):
    if config.get('Py_ENABLE_SHARED') == 0:
        return {}
    if config.get('Py_ENABLE_SHARED') != 1:
        raise ValueError('Python shared-library configuration is unavailable')
    name = config.get('INSTSONAME') or config.get('LDLIBRARY')
    return {'python-library': Path(config['LIBDIR']) / name}


def _compile_runtime_parameters(config):
    import sys

    return {
        'version': sys.version, 'implementation': sys.implementation.name,
        'cache_tag': sys.implementation.cache_tag, 'platform': sys.platform,
        'byteorder': sys.byteorder, 'flags': list(sys.flags), 'xoptions': sys._xoptions,
        'warnoptions': sys.warnoptions, 'sys_path': sys.path,
        'prefixes': [sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix],
        'configuration': config,
    }


def _compile_runtime_tree_layout(paths):
    excluded = frozenset(Path(paths[name]).resolve() for name in ('purelib', 'platlib'))
    roots = sorted({Path(paths[name]) for name in ('stdlib', 'platstdlib')})
    return roots, excluded - frozenset(root.resolve() for root in roots)


def _compile_standard_library_files(paths, deadline):
    roots, excluded = _compile_runtime_tree_layout(paths)
    for root in roots:
        prefix = _compile_identity_digest(str(root))
        for path in _compile_dependency_tree(root, deadline=deadline, excluded=excluded):
            yield f'stdlib/{prefix}/{path.relative_to(root).as_posix()}', path


def compile_runtime_dependency_manifest(*, deadline=None):
    """Inventory configured Python files and execution flags, without new limits.

    Package roots are covered by the separate package inventory. This component
    binds on-disk interpreter/stdlib bytes, not old code already loaded in memory
    or transitive operating-system libraries. Unsupported shared-library metadata
    refuses qualification explicitly; no guessed platform pathname is substituted.
    """
    import sys
    import sysconfig

    operational_deadline_active(deadline)
    paths, config = sysconfig.get_paths(), sysconfig.get_config_vars()
    files = dict(_compile_standard_library_files(paths, deadline))
    files.update(_compile_runtime_library(config))
    files['python-executable'] = Path(sys.executable)
    files.update({f'search-path/{index}': Path(path) for index, path in enumerate(sys.path)
                  if Path(path).is_file()})
    return compile_dependency_manifest(files, _compile_runtime_parameters(config), deadline=deadline)


def _compile_root_details(origin, source_digest, dependency_digest, request):
    return progress.root_details(origin, source_digest, dependency_digest,
                                 _compile_identity_digest(request))


def begin_compile_work(queue, lease, prepare):
    """Find retained original work before any live input/dependency preparation.

    prepare is called only for new work and returns source digest, dependency
    digest and invocation parameters. This never authorizes a model call itself.
    The existing queue transaction serializes publication of immutable event zero.
    """
    origin = queue.compile_origin(lease)
    work_id = progress.root_work_id(origin)
    previous = queue.get_compile_event(work_id, sequence=0)
    if previous is not None:
        queue.verify_compile_checkpoint_owner(lease, previous)
        progress.validate_root_event(previous, origin)
        return previous
    source_digest, dependency_digest, request = prepare()
    details = _compile_root_details(origin, source_digest, dependency_digest, request)
    event = progress.CompileEvent(work_id, lease.id, 0, source_digest, 'pending', 'ready',
                                  {'compile_work': details})
    queue.record_compile_event(lease, event)
    return event
