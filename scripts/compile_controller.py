"""Durable external-call boundary for the approved compile controller.

Not yet activated by CLI/MCP/nightly. The shared client prepares protected calls;
semantic callers still own complete work/dependency identity and publication.
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from functools import cache, partial
from itertools import groupby
from pathlib import Path

import compile_progress as progress
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
    queue.verify_compile_artifact(checkpoint)
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
                       prompt, system_prompt, **options):
    """Use the shared DLP/preparation path, journaling only external invocation.

    The semantic caller still owns the complete work/dependency identity. The
    protected-request hash alone does not bind source snapshots or compiler code.
    """
    from llm_client import call_candidate

    dependencies.verify()
    dispatch = partial(_journal_candidate, queue, lease, checkpoint, dependencies, descriptor)
    result = call_candidate(descriptor, prompt, system_prompt, dispatch=dispatch, **options)
    dependencies.verify()
    return result


def _journal_candidate(queue, lease, checkpoint, dependencies, descriptor, prepared):
    from reliable_memory import sha256_bytes

    request = sha256_bytes(canonical_json_bytes({
        'protected_request': prepared.request_digest, 'dependencies': dependencies.verify(),
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
    Its caller supplies a verified snapshot checkpoint and a live dependency witness.
    """

    def __init__(self, queue, lease, checkpoint, *, dependencies):
        dependencies.verify()
        _require_checkpoint(checkpoint)
        self.queue = queue
        self.lease = lease
        self.checkpoint = progress._snapshot_event(checkpoint)
        self.dependencies = dependencies

    def __call__(self, descriptor, prompt, system_prompt, **options):
        result = run_candidate_call(
            self.queue, self.lease, self.checkpoint, dependencies=self.dependencies,
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
    queue.verify_compile_artifact(answer)
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
    with zipfile.ZipFile(handle) as archive:
        reader = CompileInputArchive(archive, expected)
        for group in _snapshot_types():
            _validate_capture_records(reader, group, deadline)
    operational_deadline_active(deadline)


def _validate_capture_records(reader, group, deadline):
    for _record in reader.records(group):
        operational_deadline_active(deadline)


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
        return _save_preflight_artifact(queue, lease, event, produce, validate, deadline)


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


def _save_preflight_artifact(queue, lease, event, produce, validate, deadline):
    saved = queue.get_compile_event(event.work_id, sequence=event.sequence)
    if saved is not None:
        _verify_saved_preflight(queue, lease, event, saved, validate, deadline)
        return saved
    recovered = queue.recover_compile_artifact_validated(lease, event, validate=validate, deadline=deadline)
    if recovered is not None:
        return recovered
    return queue.save_compile_artifact_writer(lease, event, produce, deadline=deadline)


def _verify_saved_preflight(queue, lease, event, saved, validate, deadline):
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
        self.output.write(_snapshot_json(value) + b'\n')

    def add(self, kind, inputs, details):
        records = [_blob_digest(_snapshot_json(_archive_record_value(part, _blob_digest)))
                   for part in inputs.dailies]
        self._consume(records)
        self.write({'kind': kind, 'source_records': records, **details})
        self.counts[kind] += 1

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

    def __init__(self, archive, expected):
        self.archive = archive
        _require_archive_members(archive)
        data = archive.read('manifest.json')
        _require_snapshot_digest(data, expected)
        self.manifest = _decoded_archive_manifest(data)
        _require_archive_records(archive, self.manifest)

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


def _restore_archive_day(reader, records):
    blob = cache(reader.blob)
    json_blob = cache(reader.json_blob)
    for value in records:
        yield _restore_archive_value('dailies', value, blob, json_blob)


def _restore_archive_value(group, value, blob, json_blob):
    _archive_record_fields(value, _ARCHIVE_BYTES_FIELDS, blob)
    _archive_record_fields(value, _ARCHIVE_JSON_FIELDS, json_blob)
    return _restored_archive_record(_snapshot_types()[group], value)


def _restored_archive_record(kind, value):
    from compile_memory import DailySnapshot

    if kind is DailySnapshot:
        _require_original_digest({**value, 'original_content': _blob_digest(value['original_content'])})
        value = _restore_snapshot_daily(value)
    result = kind(**value)
    _require_snapshot_digest(result.content, result.sha256)
    return result


def _decoded_archive_manifest(data):
    value = json.loads(data)
    expected = {'schema', 'records'}
    if not isinstance(value, dict) or set(value) != expected or value['schema'] != _ARCHIVE_SCHEMA:
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
