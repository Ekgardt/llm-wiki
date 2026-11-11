"""The compile input archive: canonical JSON, streamed arrays, blobs and the snapshot writer."""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections.abc import Sequence
from dataclasses import asdict, fields
from functools import partial

import compile_progress as progress
from bounded_io import VerifiedByteRange, immutable_source_digest
from compile_receipts import RestoredNativeFrames, archive_sequences_equal, native_frame_from_json
from llm_wiki.compile_controller_base import (
    require_original_digest,
    require_snapshot_digest,
    snapshot_types,
)


def restore_snapshot_daily(value):
    value['original_entries'] = tuple(tuple(entry) for entry in value['original_entries'])
    value['native_frames'] = _restored_native_frames(value['native_frames'])
    return value


def _restored_native_frames(values):
    if type(values) is ArchiveJsonArray:
        return values.native_records()
    return tuple(native_frame_from_json(frame) for frame in values)


ARCHIVE_SCHEMA = 'compile-input-archive/v1'


ADMITTED_ARCHIVE_SCHEMA = 'compile-input-archive/v2'


ARCHIVE_BYTES_FIELDS = ('content', 'original_content', 'prompt_content')


ARCHIVE_JSON_FIELDS = ('original_entries', 'native_frames')


def snapshot_json(value):
    return progress.canonical(value).encode('utf-8')


def _snapshot_json_chunks(values, encode):
    yield b'['
    for index, value in enumerate(values):
        if index:
            yield b','
        yield encode(value)
    yield b']'


class SnapshotJsonArray:
    """Replay canonical items and reject changed data before publication."""

    def __init__(self, values, encode=snapshot_json):
        self.values, self.encode = values, encode
        digest = hashlib.sha256()
        self.size = 0
        for chunk in _snapshot_json_chunks(values, encode):
            self.size += len(chunk)
            digest.update(chunk)
        self.digest = digest.hexdigest()

    def __len__(self):
        return self.size

    def iter_chunks(self):
        digest = hashlib.sha256()
        size = 0
        for chunk in _snapshot_json_chunks(self.values, self.encode):
            size += len(chunk)
            digest.update(chunk)
            yield chunk
        if size != self.size or digest.hexdigest() != self.digest:
            raise ValueError('snapshot JSON metadata changed during serialization')


_JSON_STRUCTURE = re.compile(rb'["\\{}\[\],]')


class _JsonArrayStructure:
    def __init__(self):
        self.quoted, self.depth, self.escape = False, 0, -2

    def boundary(self, token, position):
        if self.quoted:
            self._string_token(token, position)
            return False
        if token == 34:
            self.quoted = True
            return False
        return self._container_token(token)

    def _string_token(self, token, position):
        if position == self.escape + 1:
            self.escape = -2
            return
        if token == 92:
            self.escape = position
            return
        self.quoted = token != 34

    def _container_token(self, token):
        self.depth += {91: 1, 123: 1, 93: -1, 125: -1}.get(token, 0)
        if self.depth < 0:
            raise progress.ProgressCorrupt('invalid snapshot metadata nesting')
        return token == 44 and self.depth == 0

    def finish(self):
        if self.quoted or self.depth:
            raise progress.ProgressCorrupt('unfinished snapshot metadata value')


def _json_structure_tokens(source):
    offset = 1
    for chunk in source.iter_chunks(1, len(source) - 1):
        for match in _JSON_STRUCTURE.finditer(chunk):
            yield chunk[match.start()], offset + match.start()
        offset += len(chunk)


def _require_json_array_container(source):
    if len(source) < 2 or source[:1] != b'[' or source[-1:] != b']':
        raise progress.ProgressCorrupt('snapshot metadata must be a canonical array')


def _json_array_spans(source):
    _require_json_array_container(source)
    state, start = _JsonArrayStructure(), 1
    for token, position in _json_structure_tokens(source):
        if state.boundary(token, position):
            yield start, position
            start = position + 1
    state.finish()
    yield from _last_json_array_span(start, len(source))


def _last_json_array_span(start, size):
    if size > 2:
        yield start, size - 1


class ArchiveJsonArray(Sequence):
    """Fully validated array with per-item payload reads from its owned range."""

    def __init__(self, source):
        source.require_active()
        self.source = source
        self.spans = tuple(_json_array_spans(source))
        self.native_spans = None
        for index in range(len(self.spans)):
            self[index]

    def __len__(self):
        self.source.require_active()
        return len(self.spans)

    def __getitem__(self, index):
        self.source.require_active()
        if isinstance(index, slice):
            return tuple(self[position] for position in range(len(self))[index])
        start, end = self.spans[index]
        data = self.source[start:end]
        value = json.loads(data)
        if snapshot_json(value) != data:
            raise progress.ProgressCorrupt('noncanonical snapshot metadata')
        return value

    def native_records(self):
        if self.native_spans is None:
            self.native_spans = tuple(_native_json_span(value) for value in self)
        return RestoredNativeFrames(self)

    def __eq__(self, other):
        return archive_sequences_equal(self, other)


def _native_json_span(value):
    frame = native_frame_from_json(value)
    return frame.byte_start, frame.byte_end


def _native_frame_json(frame):
    return snapshot_json(asdict(frame))


def blob_digest(content):
    if content is None:
        return None
    if type(content) is SnapshotJsonArray:
        return content.digest
    return _source_blob_digest(content)


def _source_blob_digest(content):
    if type(content) not in (bytes, VerifiedByteRange):
        raise ValueError('snapshot content must be bytes or a verified range')
    return immutable_source_digest(content)


def archive_record_value(record, put):
    require_snapshot_digest(record.content, record.sha256)
    value = {item.name: getattr(record, item.name) for item in fields(record)}
    if 'native_frames' in value:
        value['native_frames'] = SnapshotJsonArray(value['native_frames'], _native_frame_json)
    archive_record_fields(value, ARCHIVE_BYTES_FIELDS, put)
    require_original_digest(value)
    archive_record_fields(value, ARCHIVE_JSON_FIELDS, partial(put_snapshot_json, put))
    return value


def archive_record_fields(value, names, put):
    for name in names:
        if name in value:
            value[name] = put(value[name])


def put_snapshot_json(put, value):
    if type(value) is not SnapshotJsonArray:
        value = SnapshotJsonArray(value, snapshot_json)
    return put(value)


def _archive_manifest(inputs, put, write_record):
    records = {group: write_record_metadata(group, _metadata_rows(getattr(inputs, group), put), write_record)
               for group in snapshot_types()}
    return {'schema': ARCHIVE_SCHEMA, 'records': records}


def _metadata_rows(records, put):
    for record in records:
        yield record_metadata_row(record, put)


def record_metadata_row(record, put):
    return record.logical_path, snapshot_json(archive_record_value(record, put))


def record_manifest(records, put, write_record, *, sort_groups=()):
    metadata = {group: [] for group in snapshot_types()}
    for group, record in records:
        metadata[group].append(record_metadata_row(record, put))
    for group in sort_groups:
        metadata[group].sort(key=lambda entry: entry[0])
    digests = {group: write_record_metadata(group, rows, write_record)
               for group, rows in metadata.items()}
    return {'schema': ARCHIVE_SCHEMA, 'records': digests}


def write_record_metadata(group, rows, write_record):
    digests = []
    for index, (_, data) in enumerate(rows):
        digests.append(hashlib.sha256(data).hexdigest())
        write_record(archive_record_name(group, index), data)
    return digests


def archive_record_name(group, index):
    return f'records/{group}/{index}.json'


def discard_record(_name, _data):
    """Identity computation does not publish archive entries."""


def compile_inputs_digest(inputs):
    """Bind all record metadata and original byte digests, without base64 copies."""
    manifest = _archive_manifest(inputs, blob_digest, discard_record)
    return hashlib.sha256(snapshot_json(manifest)).hexdigest()


class SnapshotArchiveWriter:
    def __init__(self, archive):
        self.archive = archive
        self.blobs = set()

    def put(self, content):
        digest = blob_digest(content)
        if digest is None or digest in self.blobs:
            return digest
        self.write('blobs/' + digest, content)
        self.blobs.add(digest)
        return digest

    def write(self, name, data):
        # A fixed ZIP epoch makes retries byte-identical; it is not source time.
        info = zipfile.ZipInfo(name)
        info.compress_type = zipfile.ZIP_STORED
        info.file_size = len(data)
        with self.archive.open(info, 'w') as output:
            for chunk in snapshot_content_chunks(data):
                output.write(chunk)


def write_archive_snapshot(inputs, expected, output):
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        writer = SnapshotArchiveWriter(archive)
        manifest = _archive_manifest(inputs, writer.put, writer.write)
        data = snapshot_json(manifest)
        require_snapshot_digest(data, expected)
        writer.write('manifest.json', data)


CONTEXT_GROUPS = ('sources', 'targets', 'vault_files')


def snapshot_content_chunks(content):
    if type(content) is VerifiedByteRange:
        yield from content.iter_chunks(0, len(content))
        return
    if type(content) is SnapshotJsonArray:
        yield from content.iter_chunks()
        return
    yield content
