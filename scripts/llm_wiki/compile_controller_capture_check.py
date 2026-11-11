"""Validating a captured source archive and the admission it records."""
from __future__ import annotations

import hashlib
import inspect
import zipfile
from contextlib import ExitStack, closing

import compile_progress as progress
from bounded_io import require_byte_range
from llm_wiki.compile_controller_archive import (
    ARCHIVE_BYTES_FIELDS,
    ARCHIVE_JSON_FIELDS,
    archive_record_fields,
    restore_snapshot_daily,
)
from llm_wiki.compile_controller_base import require_original_digest, snapshot_types
from llm_wiki.compile_controller_reader import (
    CompileInputArchive,
    require_archive_members,
    require_capture_admission,
    shared_record_blobs,
)
from llm_wiki.sqlite_deadlines import operational_deadline_active


def read_admitted_capture_root(lease, origin, deadline, handle):
    operational_deadline_active(deadline)
    with zipfile.ZipFile(handle) as archive:
        require_archive_members(archive)
        source_digest = hashlib.sha256(archive.read('manifest.json')).hexdigest()
        reader = CompileInputArchive(archive, source_digest)
        admission = capture_admission_for_owner(reader.manifest, lease, origin)
        _validate_capture_reader(reader, deadline)
    details = progress.root_details(origin, source_digest, admission['dependency_digest'],
                                   admission['request_digest'])
    return progress.CompileEvent(progress.root_work_id(origin), lease.id, 0, source_digest,
                                 'pending', 'ready', {'compile_work': details})


def capture_admission_for_owner(manifest, lease, origin):
    admission = manifest.get('admission')
    require_capture_admission(admission)
    if (admission['origin_task_id'], admission['input_hash']) != (origin, lease.input_hash):
        raise progress.ProgressConflict('capture admission task or payload changed')
    return admission


def validate_capture_archive(expected, deadline, handle):
    operational_deadline_active(deadline)
    with zipfile.ZipFile(handle) as archive:
        reader = CompileInputArchive(archive, expected)
        _validate_capture_reader(reader, deadline)
    operational_deadline_active(deadline)


def _validate_capture_reader(reader, deadline):
    verified = set()
    for group in snapshot_types():
        _validate_capture_records(reader, group, deadline, verified)
    operational_deadline_active(deadline)


def _validate_capture_records(reader, group, deadline, verified):
    signature = inspect.signature(snapshot_types()[group])
    metadata = {}
    with closing(_capture_record_values(reader, group, deadline)) as records:
        for value, original in records:
            _validate_capture_record_bytes(reader, value, verified, deadline, original)
            metadata = shared_record_blobs(value, ARCHIVE_JSON_FIELDS, reader.json_blob, metadata)
            _validate_capture_record_shape(group, value, signature, metadata)


def _capture_record_values(reader, group, deadline):
    previous = (None, None)
    with ExitStack() as scope:
        for index in range(reader.record_count(group)):
            operational_deadline_active(deadline)
            value = reader.record_metadata(group, index)
            require_original_digest(value)
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
    progress.require_digest(value['content'])
    if value['content'] != value['sha256']:
        raise ValueError('snapshot content digest mismatch')
    _validate_original_part(value, original, verified)
    for name in ARCHIVE_BYTES_FIELDS:
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
    progress.require_digest(digest)
    if digest not in verified:
        reader.verify_blob(digest, deadline=deadline)
        verified.add(digest)


def _validate_capture_record_shape(group, value, signature, metadata):
    archive_record_fields(value, ARCHIVE_JSON_FIELDS, metadata.__getitem__)
    if group == 'dailies':
        value = restore_snapshot_daily(value)
    signature.bind(**value)
