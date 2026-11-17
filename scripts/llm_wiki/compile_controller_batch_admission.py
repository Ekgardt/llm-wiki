"""Admitting one planned batch and binding the context it runs with."""
from __future__ import annotations

import zipfile
from dataclasses import replace
from functools import partial

import compile_progress as progress
from llm_wiki.compile_controller_archive import CONTEXT_GROUPS, blob_digest
from llm_wiki.compile_controller_base import compile_identity_digest
from llm_wiki.compile_controller_context_delta import (
    read_compile_context_delta,
    write_compile_context_delta,
)
from llm_wiki.compile_controller_preflight import (
    batch_admission_event,
    require_planned_batch_record,
    require_preflight_parent,
    require_recorded_batch_parent,
    save_verified_compile_artifact,
)
from llm_wiki.compile_controller_reader import open_compile_inputs
from llm_wiki.sqlite_deadlines import operational_deadline_active


def begin_compile_batch(queue, lease, planned, *, dependencies, deadline=None):
    """Admit a retained plan entry before reading any mutable context.

    This binds immutable daily records and the complete preflight. It does not
    authorize model calls or certify that refreshed context has been saved.
    """
    operational_deadline_active(deadline)
    require_recorded_batch_parent(queue, lease, planned, dependencies)
    require_planned_batch_record(queue, planned, deadline)
    expected = batch_admission_event(lease, planned, dependencies)
    dependencies.verify()
    operational_deadline_active(deadline)
    return record_or_resume_batch_admission(queue, lease, expected)


def record_or_resume_batch_admission(queue, lease, expected):
    previous = queue.get_compile_event(expected.work_id, sequence=0)
    if previous is not None:
        return resume_batch_admission(queue, lease, previous, expected)
    queue.record_compile_event(lease, expected)
    return expected


def resume_batch_admission(queue, lease, previous, expected):
    queue.verify_compile_checkpoint_owner(lease, previous)
    if replace(previous, task_id=lease.id) != expected:
        raise progress.ProgressConflict('retained batch admission identity changed')
    return previous


def batch_context_event(queue, lease, planned, snapshot, dependencies, deadline):
    admission = begin_compile_batch(queue, lease, planned, dependencies=dependencies, deadline=deadline)
    require_preflight_parent(queue, lease, snapshot)
    if planned.preflight.details['compile_preflight']['snapshot_digest'] != progress.event_digest(snapshot):
        raise progress.ProgressConflict('batch context source snapshot changed')
    binding = {'schema': 'compile-batch-context/v1',
               'admission_digest': progress.event_digest(admission),
               'snapshot_work_id': snapshot.work_id, 'snapshot_sequence': snapshot.sequence,
               'snapshot_digest': progress.event_digest(snapshot)}
    return replace(admission, task_id=lease.id, sequence=1, stage='snapshot_ready',
                   details={'compile_context': binding})


def context_event_binding(event):
    return compile_identity_digest(event.details['compile_context'])


def capture_current_compile_context():
    from llm_wiki.compile_pipeline_sources import (
        iter_compile_records,
    )

    return iter_compile_records(())


def save_compile_batch_context(queue, lease, planned, snapshot, *, dependencies,
                               capture=capture_current_compile_context, deadline=None):
    """Recover exact context before invoking the live capture producer."""
    event = batch_context_event(queue, lease, planned, snapshot, dependencies, deadline)
    with open_compile_inputs(queue, lease, snapshot, deadline=deadline) as base:
        binding = context_event_binding(event)
        produce = partial(_write_batch_context, base, binding, dependencies, capture, deadline)
        validate = partial(_validate_batch_context, base, binding, dependencies, deadline)
        saved = save_verified_compile_artifact(queue, lease, event, produce, validate, deadline)
    dependencies.verify()
    queue.verify_compile_checkpoint_owner(lease, saved)
    return saved


def _write_batch_context(base, binding, dependencies, capture, deadline, output):
    dependencies.verify()
    write_compile_context_delta(output, base, capture(), binding=binding, deadline=deadline)
    dependencies.verify()


def read_bound_batch_context(archive, base, binding):
    manifest_digest = blob_digest(archive.read('manifest.json'))
    return read_compile_context_delta(archive, manifest_digest, base, binding=binding)


def validate_context_view(view, deadline):
    for group in CONTEXT_GROUPS:
        for _record in getattr(view, group):
            operational_deadline_active(deadline)


def _validate_batch_context(base, binding, dependencies, deadline, handle):
    with zipfile.ZipFile(handle) as archive:
        view = read_bound_batch_context(archive, base, binding)
        validate_context_view(view, deadline)
    dependencies.verify()
    operational_deadline_active(deadline)
