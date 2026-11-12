"""Repair batches for a refused publication, and the proof that their children completed."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from functools import partial

import compile_progress as progress
from llm_wiki.compile_call_runtime import require_checkpoint
from llm_wiki.compile_controller_archive import snapshot_json
from llm_wiki.compile_controller_base import compile_identity_digest
from llm_wiki.compile_controller_batch_admission import (
    record_or_resume_batch_admission,
    resume_batch_admission,
)
from llm_wiki.compile_controller_calls import (
    CompileReplay,
    matched_compile_checkpoint,
    record_or_read_compile_checkpoint,
)
from llm_wiki.compile_controller_preflight import (
    batch_source_scope,
    preflight_batch_details,
    preflight_source_records,
    save_verified_compile_artifact,
)
from llm_wiki.compile_controller_publication import (
    append_or_match_compile_completion,
    check_batch_context_owner,
    compile_work_cursor,
    confirm_publication_under_gate,
    execute_compile_publication,
    require_publication_context,
    require_publication_coordinator,
    save_compile_publication,
)
from llm_wiki.sqlite_deadlines import operational_deadline_active


def _resolve_compile_publication(queue, lease, context, batch, *, coordinator, dependencies,
                                 delivery, deadline, critic_feedback=()):
    current = compile_work_cursor(queue, context.work_id)
    if current.stage == 'publication_ready':
        return current
    if 'compile_repair' in current.details:
        return _raise_retained_compile_repair(queue, lease, current, context, batch, dependencies, deadline)
    return _resolve_new_compile_publication(queue, lease, context, batch, coordinator,
                                            dependencies, delivery, deadline, critic_feedback)


def _resolve_new_compile_publication(queue, lease, context, batch, coordinator, dependencies,
                                     delivery, deadline, critic_feedback):
    from compile_cache import CompileCache
    from llm_wiki.compile_pipeline_batches import (
        CompileRepairCapacityError,
        resolve_compile_plan,
    )

    replay = CompileReplay(queue, lease, context, dependencies=dependencies, delivery=delivery)
    try:
        resolved = resolve_compile_plan(batch.inputs, CompileCache(queue.state_root),
            coordinator=coordinator, batch=batch, call_runner=replay, critic_feedback=critic_feedback)
    except CompileRepairCapacityError as failure:
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
    from llm_wiki.compile_pipeline_batches import (
        CompileRepairCapacityError,
    )

    if not isinstance(failure, CompileRepairCapacityError):
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
            'context_digest': progress.event_digest(context),
            'batch_digest': compile_identity_digest(preflight_batch_details(batch)),
            'failure_digest': compile_identity_digest(failure), 'dependency_digest': dependencies.digest}


def _repair_checkpoint(queue, lease, context, binding):
    latest = compile_work_cursor(queue, context.work_id)
    queue.verify_compile_checkpoint_owner(lease, latest)
    if 'compile_repair' in latest.details:
        return _existing_repair_checkpoint(latest, binding)
    require_checkpoint(latest)
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
    remaining = Counter(preflight_source_records(batch.inputs))
    for child, _feedback in children:
        _require_repair_child(batch, child)
        remaining.subtract(preflight_source_records(child.inputs))
    if any(remaining.values()):
        raise progress.ProgressCorrupt('repair source coverage changed')


def _require_repair_child(parent, child):
    from llm_wiki.compile_pipeline_batches import (
        require_repair_context,
    )

    if not 0 < len(child.inputs.dailies) < len(parent.inputs.dailies):
        raise progress.ProgressCorrupt('repair child does not reduce complete source work')
    require_repair_context(parent, child)
    if (child.inputs.targets, child.inputs.vault_files) != (parent.inputs.targets, parent.inputs.vault_files):
        raise progress.ProgressCorrupt('repair child changed publication context')


def _repair_child_record(child, feedback):
    return {'source_records': preflight_source_records(child.inputs),
            **preflight_batch_details(child), 'feedback': list(feedback)}


def _repair_document(event, batch, failure):
    from compile_pipeline import (
        repartition_compile_repair,
    )

    children = repartition_compile_repair(batch, tuple(failure['feedback']))
    _require_repair_partition(batch, children)
    records = [_repair_child_record(child, feedback) for child, feedback in children]
    document = {'schema': 'compile-repair-document/v1', 'binding': event.details['compile_repair'],
                'failure': failure, 'children': records}
    return document, children


def _write_repair_document(document, dependencies, deadline, output):
    dependencies.verify()
    operational_deadline_active(deadline)
    output.write(snapshot_json(document))
    dependencies.verify()
    operational_deadline_active(deadline)


def _validate_repair_document(document, dependencies, deadline, handle):
    if handle.read() != snapshot_json(document):
        raise progress.ProgressCorrupt('retained repair partition changed')
    dependencies.verify()
    operational_deadline_active(deadline)


def save_compile_repair(queue, lease, context, batch, failure, *, dependencies, deadline=None):
    """Retain a capacity repair decision before admitting any child model call."""
    operational_deadline_active(deadline)
    require_publication_context(queue, lease, context, batch, dependencies)
    content = _repair_failure_content(failure)
    binding = _repair_binding(context, batch, content, dependencies)
    event = _repair_checkpoint(queue, lease, context, binding)
    document, _children = _repair_document(event, batch, content)
    saved = save_verified_compile_artifact(queue, lease, event,
        partial(_write_repair_document, document, dependencies, deadline),
        partial(_validate_repair_document, document, dependencies, deadline), deadline)
    check_batch_context_owner(queue, lease, saved, dependencies, deadline)
    return saved


def _decoded_repair_document(event, raw):
    document = json.loads(raw)
    fields = {'schema', 'binding', 'failure', 'children'}
    if not isinstance(document, dict) or set(document) != fields:
        raise progress.ProgressCorrupt('invalid repair document fields')
    expected = ('compile-repair-document/v1', event.details['compile_repair'], raw)
    if (document['schema'], document['binding'], snapshot_json(document)) != expected:
        raise progress.ProgressCorrupt('repair document identity changed')
    _require_repair_failure_content(document['failure'])
    return document


def _repair_child_identity(event, batch, feedback, record):
    digest = compile_identity_digest(record)
    work_id = compile_identity_digest({'schema': 'compile-repair-child/v1',
        'repair_digest': progress.event_digest(event), 'entry_digest': digest})
    return RepairCompileBatch(batch, tuple(feedback), progress.snapshot_event(event), digest, work_id)


def read_compile_repair(queue, lease, event, context, batch, *, dependencies, deadline=None):
    """Reconstruct checked children while the caller keeps the parent context open."""
    document, children = _read_verified_compile_repair(queue, lease, event, context, batch, dependencies, deadline)
    return tuple(_repair_child_identity(event, child, feedback, record)
                 for (child, feedback), record in zip(children, document['children']))


def _raise_retained_compile_repair(queue, lease, event, context, batch, dependencies, deadline):
    from llm_wiki.compile_pipeline_batches import (
        CompileRepairCapacityError,
    )

    document, _children = _read_verified_compile_repair(queue, lease, event, context, batch, dependencies, deadline)
    raise CompileRepairCapacityError(document['failure']['reason'], tuple(document['failure']['feedback']))


def _read_verified_compile_repair(queue, lease, event, context, batch, dependencies, deadline):
    require_publication_context(queue, lease, context, batch, dependencies)
    _require_recorded_repair(queue, lease, event)
    with queue.open_compile_artifact(event, deadline=deadline) as handle:
        document = _decoded_repair_document(event, handle.read())
    _existing_repair_checkpoint(event, _repair_binding(context, batch, document['failure'], dependencies))
    expected, children = _repair_document(event, batch, document['failure'])
    if document != expected:
        raise progress.ProgressCorrupt('retained repair partition changed')
    check_batch_context_owner(queue, lease, event, dependencies, deadline)
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
               'repair_sequence': parent.sequence, 'repair_digest': progress.event_digest(parent),
               'entry_digest': child.entry_digest, 'dependency_digest': dependencies.digest}
    source_digest = compile_identity_digest(preflight_source_records(child.batch.inputs))
    return progress.CompileEvent(child.work_id, lease.id, 0, source_digest,
                                 'pending', 'ready', {'compile_batch': binding,
                                 'source_scope': batch_source_scope(child.batch)})


def _repair_child_context(lease, admission, parent_context):
    binding = {'schema': 'compile-repair-context/v1',
               'admission_digest': progress.event_digest(admission),
               'parent_work_id': parent_context.work_id, 'parent_sequence': parent_context.sequence,
               'parent_digest': progress.event_digest(parent_context)}
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
    check_batch_context_owner(queue, lease, child.repair, dependencies, deadline)
    admitted = record_or_resume_batch_admission(queue, lease, expected)
    context = _repair_child_context(lease, admitted, parent_context)
    saved = record_or_read_compile_checkpoint(queue, lease, context)
    check_batch_context_owner(queue, lease, saved, dependencies, deadline)
    return saved


def execute_repair_compile_batch(queue, lease, child, parent_context, parent_batch, *, coordinator,
                                 dependencies, trigger, owner=None, deadline=None, cancelled=None, delivery=None):
    """Execute one admitted child while its caller owns the parent archive scope."""
    from compile_receipts import (
        require_compile_active,
    )

    require_publication_coordinator(queue, coordinator)
    compiler_deadline = float('inf') if deadline is None else deadline
    require_compile_active(compiler_deadline, cancelled)
    if delivery is not None:
        delivery.require_owner(queue, lease)
    context = begin_compile_repair_batch(queue, lease, child, parent_context, parent_batch,
                                         dependencies=dependencies, deadline=deadline)
    return execute_context_compile_batch(queue, lease, context, child.batch, coordinator=coordinator,
        dependencies=dependencies, trigger=trigger, owner=owner, deadline=deadline,
        cancelled=cancelled, delivery=delivery, critic_feedback=child.feedback)


def execute_context_compile_batch(queue, lease, context, batch, *, coordinator, dependencies,
                                   trigger, owner, deadline, cancelled, delivery, critic_feedback):
    from compile_receipts import (
        require_compile_active,
    )

    compiler_deadline = float('inf') if deadline is None else deadline
    require_compile_active(compiler_deadline, cancelled)
    publication = _resolve_compile_publication(queue, lease, context, batch,
        coordinator=coordinator, dependencies=dependencies, delivery=delivery, deadline=deadline,
        critic_feedback=critic_feedback)
    require_compile_active(compiler_deadline, cancelled)
    return execute_compile_publication(queue, lease, publication, batch, coordinator=coordinator,
        dependencies=dependencies, trigger=trigger, owner=owner, deadline=deadline, cancelled=cancelled)


def _read_repair_child_context(queue, lease, child, parent_context, dependencies, deadline):
    admitted = queue.get_compile_event(child.work_id, sequence=0)
    if admitted is None:
        return None
    resume_batch_admission(queue, lease, admitted, _repair_child_admission(lease, child, dependencies))
    saved = queue.get_compile_event(child.work_id, sequence=1)
    if saved is None:
        return None
    expected = _repair_child_context(lease, admitted, parent_context)
    matched_compile_checkpoint(queue, lease, saved, expected)
    queue.verify_compile_artifact(saved, deadline=deadline)
    return saved


@dataclass(frozen=True)
class _RepairProofFrame:
    repair: progress.CompileEvent
    context: progress.CompileEvent
    batch: object
    children: tuple | None = None


@dataclass
class RepairProof:
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
        cursor = compile_work_cursor(self.queue, child.work_id)
        if 'compile_repair' in cursor.details:
            stack.append(_RepairProofFrame(cursor, context, child.batch))
            return
        self.results[child.work_id] = self._leaf(cursor, child.batch)

    def _leaf(self, cursor, batch):
        if cursor.stage != 'publication_ready':
            return None
        return confirm_publication_under_gate(self.queue, self.lease, cursor, batch,
            self.coordinator, self.dependencies, self.deadline, self.cancelled)

    def _finish(self, frame):
        self._check_active()
        check_batch_context_owner(self.queue, self.lease, frame.repair, self.dependencies, self.deadline)
        children = tuple(self.results[child.work_id] for child in frame.children)
        self.results[frame.repair.work_id] = _record_repair_completion(self.queue, self.lease, frame.repair, children)

    def _check_active(self):
        from compile_receipts import (
            require_compile_active,
        )

        require_compile_active(float('inf') if self.deadline is None else self.deadline, self.cancelled)


def _record_repair_completion(queue, lease, repair, children):
    previous = queue.get_compile_event(repair.work_id, sequence=repair.sequence + 1)
    if not children or any(child is None for child in children):
        return _missing_repair_completion(previous)
    expected = _repair_completion_event(lease, repair, children)
    return append_or_match_compile_completion(queue, lease, previous, expected)


def _missing_repair_completion(previous):
    if previous is not None:
        raise progress.ProgressCorrupt('recorded repair completion has incomplete source receipts')
    return None


def _repair_completion_event(lease, repair, children):
    proof = {'schema': 'compile-repair-completion/v1', 'repair_digest': progress.event_digest(repair),
             'children': [{'work_id': child.work_id, 'sequence': child.sequence,
                           'event_digest': progress.event_digest(child)} for child in children]}
    return replace(repair, task_id=lease.id, sequence=repair.sequence + 1,
                   stage='published', outcome='completed', details={'repair_completion': proof})


def confirm_compile_repair(queue, lease, repair, context, batch, *, coordinator, dependencies,
                           owner=None, deadline=None, cancelled=None):
    """Prove every descendant receipt in one cooperating-writer snapshot; never run work."""
    from llm_wiki.compile_pipeline_receipts import (
        compile_writer_gate,
    )

    require_publication_coordinator(queue, coordinator)
    compiler_deadline = float('inf') if deadline is None else deadline
    with compile_writer_gate(coordinator, owner, compiler_deadline, cancelled):
        return RepairProof(queue, lease, coordinator, dependencies, deadline, cancelled).run(repair, context, batch)
