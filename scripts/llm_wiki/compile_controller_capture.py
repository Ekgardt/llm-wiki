"""Capturing the exact source snapshot once and admitting it with its root work event."""
from __future__ import annotations

import zipfile
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

import compile_progress as progress
from llm_wiki import runtime_roots
from llm_wiki.compile_controller_archive import (
    ADMITTED_ARCHIVE_SCHEMA,
    SnapshotArchiveWriter,
    compile_inputs_digest,
    snapshot_json,
    write_archive_snapshot,
)
from llm_wiki.compile_controller_base import compile_identity_digest, require_input_checkpoint
from llm_wiki.compile_controller_capture_check import (
    capture_admission_for_owner,
    read_admitted_capture_root,
    validate_capture_archive,
)
from llm_wiki.compile_controller_dependencies import CompileDependencies
from llm_wiki.compile_controller_preflight import require_preflight_parent
from llm_wiki.compile_controller_reader import (
    capture_manifest,
    open_compile_inputs,
    write_compile_capture,
)
from llm_wiki.sqlite_deadlines import operational_deadline_active


def save_compile_inputs(queue, lease, checkpoint, inputs):
    """Write record-addressable inputs without a whole-archive bytes object."""
    require_input_checkpoint(checkpoint)
    if compile_inputs_digest(inputs) != checkpoint.source_digest:
        raise ValueError('snapshot content digest mismatch')
    producer = partial(write_archive_snapshot, inputs, checkpoint.source_digest)
    return queue.save_compile_artifact_writer(lease, checkpoint, producer)


def save_compile_capture(queue, lease, checkpoint, paths, *, compiled=None, deadline=None):
    """Save or recover the exact admitted source snapshot without materializing it."""
    checkpoint = _unbound_capture_checkpoint(checkpoint)
    _require_capture_parent(queue, lease, checkpoint)
    saved = _recorded_capture(queue, lease, checkpoint, deadline)
    if saved is not None:
        return saved
    validate = partial(validate_capture_archive, checkpoint.source_digest, deadline)
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
    decoder = partial(read_admitted_capture_root, lease, origin, deadline)
    return queue.publish_initial_compile_capture(lease, producer=producer,
                                                 decode_root=decoder, deadline=deadline)


def _resume_admitted_capture(queue, lease, root, origin, deadline):
    progress.validate_root_event(root, origin)
    queue.verify_compile_checkpoint_owner(lease, root)
    checkpoint = replace(root, task_id=lease.id, sequence=1, stage='snapshot_ready', details={})
    saved = _recorded_capture(queue, lease, checkpoint, deadline)
    if saved is not None:
        return saved
    validate = partial(validate_capture_archive, checkpoint.source_digest, deadline)
    recovered = queue.recover_compile_artifact_validated(lease, checkpoint, validate=validate, deadline=deadline)
    if recovered is None:
        raise progress.ProgressConflict('retained compile work has no recoverable source snapshot')
    return recovered


def require_capture_vault(queue):
    expected = tuple(Path(path).resolve(strict=True) for path in (queue.vault, queue.state_root))
    actual = tuple(path.resolve(strict=True) for path in (runtime_roots.vault_root(), runtime_roots.state_root()))
    if actual != expected:
        raise progress.ProgressConflict('capture vault identity differs from queue')


def _write_admitted_capture(queue, lease, origin, prepare, deadline, output):
    require_capture_vault(queue)
    operational_deadline_active(deadline)
    prepared = prepare()
    binding = _capture_admission_binding(lease, origin, prepared)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        writer = SnapshotArchiveWriter(archive)
        manifest = capture_manifest(prepared.paths, prepared.compiled, writer.put, writer.write)
        prepared.dependencies.verify()
        manifest.update(schema=ADMITTED_ARCHIVE_SCHEMA, admission=binding)
        writer.write('manifest.json', snapshot_json(manifest))


def _capture_admission_binding(lease, origin, prepared):
    return {'schema': 'compile-capture-admission/v2', 'origin_task_id': origin,
            'input_hash': lease.input_hash, 'dependency_digest': prepared.dependencies.verify(),
            'request_digest': compile_identity_digest(prepared.request),
            'request': prepared.request, 'execution': prepared.execution}


def read_compile_execution(queue, lease, snapshot, *, deadline=None):
    """Read execution inputs from the owned immutable admission, without discovery."""
    require_preflight_parent(queue, lease, snapshot)
    with open_compile_inputs(queue, lease, snapshot, deadline=deadline) as reader:
        admission = capture_admission_for_owner(reader.manifest, lease, queue.compile_origin(lease))
        if admission['schema'] != 'compile-capture-admission/v2' or admission['execution'] is None:
            raise progress.ProgressConflict('retained compile work has no execution planning evidence')
        result = admission['request'], admission['execution']
    queue.verify_compile_checkpoint_owner(lease, snapshot)
    operational_deadline_active(deadline)
    return result


def _unbound_capture_checkpoint(checkpoint):
    checkpoint = progress.snapshot_event(checkpoint)
    require_input_checkpoint(checkpoint)
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
    require_capture_event_identity(checkpoint, saved)
    with queue.open_compile_artifact(saved, deadline=deadline) as handle:
        validate_capture_archive(checkpoint.source_digest, deadline, handle)
    queue.verify_compile_checkpoint_owner(lease, saved)
    return saved


def require_capture_event_identity(checkpoint, saved):
    details = dict(saved.details)
    details.pop('artifact', None)
    if replace(saved, task_id=checkpoint.task_id, details=details) != checkpoint:
        raise progress.ProgressConflict('capture checkpoint identity changed')


def _write_checked_capture(paths, expected, compiled, output):
    actual = write_compile_capture(output, paths, compiled=compiled)
    if actual != expected:
        raise ValueError('captured snapshot digest changed before publication')
