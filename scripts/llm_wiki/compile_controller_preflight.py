"""The saved compile preflight and the planned batches it promises."""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, replace
from functools import partial

import compile_progress as progress
from llm_wiki.compile_controller_archive import archive_record_value, blob_digest, snapshot_json
from llm_wiki.compile_controller_base import compile_identity_digest, require_input_checkpoint
from llm_wiki.compile_controller_reader import open_compile_inputs
from llm_wiki.sqlite_deadlines import operational_deadline_active


def save_compile_preflight(queue, lease, snapshot, *, dependencies, request, plan, deadline=None):
    """Persist a complete metadata plan against the immutable source snapshot.

    `plan(reader, on_refused)` yields CompileBatch values without model calls.
    The caller's dependency witness and request must bind its effective planning
    configuration. Recovery recomputes local planning from saved bytes, never
    mutable live sources; this API neither dispatches nor authorizes model work.
    """
    operational_deadline_active(deadline)
    snapshot = progress.snapshot_event(snapshot)
    root = require_preflight_parent(queue, lease, snapshot)
    dependencies.verify()
    event = _preflight_event(lease, snapshot, dependencies, request)
    _require_preflight_work_identity(root, event)
    with open_compile_inputs(queue, lease, snapshot, deadline=deadline) as reader:
        produce = partial(_write_compile_preflight, reader, event.details, dependencies, plan, deadline)
        validate = partial(_validate_compile_preflight, produce, deadline)
        return save_verified_compile_artifact(queue, lease, event, produce, validate, deadline)


def require_preflight_parent(queue, lease, snapshot):
    require_input_checkpoint(snapshot)
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
               'snapshot_digest': progress.event_digest(snapshot),
               'dependency_digest': dependencies.digest,
               'request_digest': compile_identity_digest(request)}
    return replace(snapshot, task_id=lease.id, sequence=snapshot.sequence + 1,
                   stage='pending', outcome='ready', details={'compile_preflight': binding})


def save_verified_compile_artifact(queue, lease, event, produce, validate, deadline):
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
        data = snapshot_json(value)
        self.output.write(data + b'\n')
        return blob_digest(data)

    def add(self, kind, inputs, details):
        records = preflight_source_records(inputs)
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


def preflight_batch_details(batch):
    sources = [archive_record_value(source, blob_digest) for source in batch.inputs.sources]
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
        writer.add('batch', batch.inputs, preflight_batch_details(batch))
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
    yield from iter_saved_compile_preflight(queue, lease, snapshot, saved, dependencies,
                                             plan, on_refused, deadline)


def iter_saved_compile_preflight(queue, lease, snapshot, saved, dependencies, plan, on_refused, deadline):
    check = partial(check_preflight_consumer, queue, lease, saved, dependencies, deadline)
    with open_compile_inputs(queue, lease, snapshot, deadline=deadline) as reader:
        with queue.open_compile_artifact(saved, deadline=deadline) as handle:
            yield from _consume_compile_preflight(reader, handle, saved, plan, on_refused, check, deadline)


def check_preflight_consumer(queue, lease, saved, dependencies, deadline):
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
        entry_digest = writer.add('batch', batch.inputs, preflight_batch_details(batch))
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
                'preflight_digest': progress.event_digest(preflight),
                'entry_digest': entry_digest}
    return PlannedCompileBatch(batch, progress.snapshot_event(preflight), entry_digest,
                               compile_identity_digest(identity), entry_offset)


def preflight_source_records(inputs):
    return [blob_digest(snapshot_json(archive_record_value(part, blob_digest)))
            for part in inputs.dailies]


def _planned_batch_record(planned):
    value = {'kind': 'batch', 'source_records': preflight_source_records(planned.batch.inputs),
             **preflight_batch_details(planned.batch)}
    data = snapshot_json(value)
    if blob_digest(data) != planned.entry_digest:
        raise progress.ProgressConflict('planned batch entry identity changed')
    return data


def require_recorded_batch_parent(queue, lease, planned, dependencies):
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


def require_planned_batch_record(queue, planned, deadline):
    _require_planned_batch_address(planned)
    data = _planned_batch_record(planned)
    with queue.open_compile_artifact(planned.preflight, deadline=deadline) as handle:
        handle.seek(planned.entry_offset)
        if handle.read(len(data) + 1) != data + b'\n':
            raise progress.ProgressCorrupt('planned batch is absent from retained preflight')


def batch_admission_event(lease, planned, dependencies):
    parent = planned.preflight
    details = {'schema': 'compile-batch/v1', 'preflight_work_id': parent.work_id,
               'preflight_sequence': parent.sequence,
               'preflight_digest': progress.event_digest(parent),
               'entry_digest': planned.entry_digest, 'entry_offset': planned.entry_offset,
               'dependency_digest': dependencies.digest}
    sources = compile_identity_digest(preflight_source_records(planned.batch.inputs))
    return progress.CompileEvent(planned.work_id, lease.id, 0, sources,
                                 'pending', 'ready', {'compile_batch': details,
                                 'source_scope': batch_source_scope(planned.batch)})


def batch_source_scope(batch):
    return progress.make_source_scope(_batch_source_span(part) for part in batch.inputs.dailies)


def _batch_source_span(part):
    from llm_wiki.compile_pipeline_receipts import (
        v4_source_descriptor,
    )

    source = v4_source_descriptor(part)
    return {key: source[key] for key in ('logical_path', 'byte_start', 'byte_end', 'sha256')}
