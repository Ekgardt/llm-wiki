"""Reading a saved compile input archive back, every record and original checked against its digest."""
from __future__ import annotations

import hashlib
import json
import weakref
import zipfile
from collections.abc import Sequence
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from functools import partial
from itertools import groupby

import compile_progress as progress
from bounded_io import IO_CHUNK_BYTES, VerifiedByteRange, immutable_source_digest
from llm_wiki.compile_controller_archive import (
    ADMITTED_ARCHIVE_SCHEMA,
    ARCHIVE_BYTES_FIELDS,
    ARCHIVE_JSON_FIELDS,
    ARCHIVE_SCHEMA,
    CONTEXT_GROUPS,
    ArchiveJsonArray,
    SnapshotArchiveWriter,
    archive_record_fields,
    archive_record_name,
    blob_digest,
    discard_record,
    record_manifest,
    restore_snapshot_daily,
    snapshot_json,
)
from llm_wiki.compile_controller_base import (
    compile_identity_digest,
    require_input_checkpoint,
    require_original_digest,
    require_snapshot_digest,
    snapshot_types,
)
from llm_wiki.sqlite_deadlines import operational_deadline_active


def require_capture_admission(admission):
    _require_capture_admission_fields(admission)
    progress.root_work_id(admission['origin_task_id'])
    for name in ('input_hash', 'dependency_digest', 'request_digest'):
        progress.require_digest(admission[name])
    if admission['schema'] == 'compile-capture-admission/v2':
        _require_capture_execution(admission)


def _require_capture_execution(admission):
    if compile_identity_digest(admission['request']) != admission['request_digest']:
        raise progress.ProgressCorrupt('capture request digest changed')
    if admission['execution'] is not None and not isinstance(admission['execution'], dict):
        raise progress.ProgressCorrupt('invalid capture execution planning')


def _require_capture_admission_fields(admission):
    fields = {'schema', 'origin_task_id', 'input_hash', 'dependency_digest', 'request_digest'}
    versions = {'compile-capture-admission/v1': fields,
                'compile-capture-admission/v2': fields | {'request', 'execution'}}
    if not isinstance(admission, dict) or set(admission) != versions.get(admission.get('schema')):
        raise progress.ProgressCorrupt('invalid capture admission fields')


def _stream_snapshot_digest(stream, deadline):
    digest = hashlib.sha256()
    while True:
        operational_deadline_active(deadline)
        chunk = stream.read(IO_CHUNK_BYTES)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


@contextmanager
def open_compile_inputs(queue, lease, checkpoint, *, deadline=None):
    """Own a verified snapshot reader; callers load only the records they need."""
    require_input_checkpoint(checkpoint)
    queue.verify_compile_checkpoint_owner(lease, checkpoint)
    with (
        queue.open_compile_artifact(checkpoint, deadline=deadline) as handle,
        zipfile.ZipFile(handle) as archive,
        closing(CompileInputArchive(archive, checkpoint.source_digest, deadline=deadline)) as reader,
    ):
        yield reader


def compile_capture_digest(paths, *, compiled=None, deadline=None):
    """Compute the source identity without retaining captured source bodies."""
    operational_deadline_active(deadline)
    put = partial(_deadline_blob_digest, deadline)
    manifest = capture_manifest(paths, compiled, put, discard_record)
    result = hashlib.sha256(snapshot_json(manifest)).hexdigest()
    operational_deadline_active(deadline)
    return result


def _deadline_blob_digest(deadline, content):
    operational_deadline_active(deadline)
    return blob_digest(content)


def capture_manifest(paths, compiled, put, write):
    from llm_wiki.compile_pipeline_sources import (
        iter_compile_records,
    )

    with closing(iter_compile_records(paths, compiled=compiled)) as records:
        return record_manifest(records, put, write, sort_groups=('sources', 'targets'))


def write_compile_capture(output, paths, *, compiled=None):
    """Stream source capture; return identity only after a complete container.

    The caller owns durable staging and queue/work admission. Partial output
    after an exception must never be published as a ready snapshot.
    """
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        writer = SnapshotArchiveWriter(archive)
        manifest = capture_manifest(paths, compiled, writer.put, writer.write)
        data = snapshot_json(manifest)
        writer.write('manifest.json', data)
    return hashlib.sha256(data).hexdigest()


class CompileInputArchive:
    """Context-owned record reader; opening keeps metadata, not source bodies."""

    def __init__(self, archive, expected, *, manifest_decoder=None, deadline=None):
        self.archive = archive
        require_archive_members(archive)
        data = archive.read('manifest.json')
        require_snapshot_digest(data, expected)
        decoder = manifest_decoder or _decoded_archive_manifest
        self.manifest = decoder(data)
        _require_archive_records(archive, self.manifest)
        self.digest = expected
        self.deadline = deadline
        self._originals = weakref.WeakValueDictionary()
        self._originals_closer = weakref.finalize(self, _close_original_sources, self._originals)

    def close(self):
        self._originals_closer()

    def original_source(self, digest, *, logical_path):
        if digest is None:
            return None
        self._require_open()
        key = logical_path, digest
        source = self._originals.get(key)
        if source is None:
            source = self._open_original_source(digest)
            self._originals[key] = source
        source.require_active()
        return source

    def _require_open(self):
        operational_deadline_active(self.deadline)
        if not self._originals_closer.alive or self.archive.fp is None:
            raise ValueError('source archive reader is closed')

    def _open_original_source(self, digest):
        progress.require_digest(digest)
        name = 'blobs/' + digest
        stream = self.archive.open(name)
        try:
            source = VerifiedByteRange(stream, self.archive.getinfo(name).file_size, digest, self.deadline,
                chunk_size=IO_CHUNK_BYTES, owner_check=partial(_require_original_archive_open, self.archive, stream, self._originals_closer))
        except BaseException:
            stream.close()
            raise
        weakref.finalize(source, stream.close)
        return source

    def record_count(self, group):
        return len(self.manifest['records'][group])

    def record_metadata(self, group, index):
        self._require_open()
        expected = self.manifest['records'][group][index]
        data = self.archive.read(archive_record_name(group, index))
        require_snapshot_digest(data, expected)
        value = json.loads(data)
        if snapshot_json(value) != data:
            raise progress.ProgressCorrupt('noncanonical snapshot record')
        return value

    def blob(self, digest):
        self._require_open()
        if digest is None:
            return None
        progress.require_digest(digest)
        content = self.archive.read('blobs/' + digest)
        require_snapshot_digest(content, digest)
        return content

    def verify_blob(self, digest, *, deadline=None):
        """Validate every byte through EOF without retaining the blob body."""
        self._require_open()
        progress.require_digest(digest)
        operational_deadline_active(deadline)
        with self.archive.open('blobs/' + digest) as stream:
            actual = _stream_snapshot_digest(stream, deadline)
        if actual != digest:
            raise ValueError('snapshot content digest mismatch')
        operational_deadline_active(deadline)

    @contextmanager
    def original_range(self, digest, *, deadline=None):
        self._require_open()
        progress.require_digest(digest)
        operational_deadline_active(deadline)
        name = 'blobs/' + digest
        size = self.archive.getinfo(name).file_size
        with self.archive.open(name) as stream:
            source = VerifiedByteRange(stream, size, digest, deadline, chunk_size=IO_CHUNK_BYTES,
                owner_check=partial(_require_original_archive_open, self.archive, stream, self._originals_closer))
            self._originals[(None, id(source))] = source
            yield source

    def json_blob(self, digest):
        source = self.original_source(digest, logical_path="<snapshot-metadata>")
        return ArchiveJsonArray(source)

    def record(self, group, index):
        value = self.record_metadata(group, index)
        if group == 'dailies':
            with closing(_restore_archive_day(self, (value,))) as records:
                return next(records)
        return _restore_archive_value(group, value, self.blob, self.json_blob)

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
        from llm_wiki.compile_pipeline_batches import (
            iter_streamed_compile_batches,
        )

        required = self.required_context()
        yield from iter_streamed_compile_batches(
            self.daily_records(), self.batch_context_inputs(), required,
            model=model, token_adapters=token_adapters, budget=budget,
            planning_candidates=planning_candidates, context_pending=context_pending,
            on_refused=on_refused,
        )

    def batch_context_inputs(self):
        from llm_wiki.compile_pipeline_sources import (
            CompileInputs,
        )

        pending = {self.record_metadata('dailies', index)['logical_path']
                   for index in range(self.record_count('dailies'))}
        sources = ArchiveRecords(self, 'sources')
        context = sources.select_paths(set(sources.logical_paths()) - pending)
        return CompileInputs((), context, ArchiveRecords(self, 'targets'),
                             ArchiveRecords(self, 'vault_files'))

    def required_context(self):
        from llm_wiki.compile_pipeline_batches import (
            RequiredCitationContext,
        )
        from llm_wiki.compile_pipeline_sources import (
            CompileInputs,
        )

        inputs = CompileInputs((), ArchiveRecords(self, 'sources'), ())
        return RequiredCitationContext(inputs, None, daily_records=self.daily_records)

    def planning_inputs(self):
        from llm_wiki.compile_pipeline_sources import (
            CompileInputs,
        )

        return CompileInputs(tuple(_eager_daily_records(self.daily_records())),
                             ArchiveRecords(self, 'sources'), ArchiveRecords(self, 'targets'),
                             ArchiveRecords(self, 'vault_files'))

    def materialize(self):
        from llm_wiki.compile_pipeline_sources import (
            CompileInputs,
        )

        return CompileInputs(tuple(_eager_daily_records(self.daily_records())),
                             **{group: tuple(self.records(group)) for group in CONTEXT_GROUPS})


def _require_original_archive_open(archive, stream, owner):
    if archive.fp is None or not owner.alive:
        stream.close()
        raise ValueError('source archive is closed')


def _close_original_sources(sources):
    with ExitStack() as owners:
        for source in tuple(sources.values()):
            owners.callback(source.stream.close)
        sources.clear()


def _eager_daily_records(records):
    """Explicit eager API only; normal planning keeps its verified ranges."""
    current, content = None, None
    for record in records:
        original = record.original_content
        if type(original) is VerifiedByteRange:
            content = original[:] if current is not original else content
            current = original
            record = replace(record, original_content=content)
        yield replace(record, native_frames=tuple(record.native_frames))


@dataclass(frozen=True, eq=False)
class OriginalDigestProof:
    """Computed identity bound to one immutable bytes object or verified range."""

    content: bytes | VerifiedByteRange | None = dataclass_field(repr=False)
    digest: str | None = dataclass_field(init=False)

    def __post_init__(self):
        object.__setattr__(self, 'digest', blob_digest(self.content))


def _reuse_original_proof(previous, content):
    if type(previous) is OriginalDigestProof and previous.content is content:
        return previous
    return OriginalDigestProof(content)


def _proven_original_digest(content, proof):
    if type(content) is VerifiedByteRange:
        return immutable_source_digest(content)
    return _proven_byte_original_digest(content, proof)


def _proven_byte_original_digest(content, proof):
    if proof is None:
        return blob_digest(content)
    if type(proof) is not OriginalDigestProof or proof.content is not content:
        raise ValueError('original digest proof does not own these bytes')
    return proof.digest


def _restore_archive_day(reader, records):
    originals, metadata = {}, {}
    proof = None
    for value in records:
        read_original = partial(reader.original_source, logical_path=value['logical_path'])
        originals = shared_record_blobs(value, ('original_content',), read_original, originals)
        metadata = shared_record_blobs(value, ARCHIVE_JSON_FIELDS, reader.json_blob, metadata)
        proof = _reuse_original_proof(proof, originals.get(value.get('original_content')))
        blob = reader.blob
        json_blob = partial(_read_shared_blob, reader.json_blob, metadata)
        yield _restore_archive_value('dailies', value, blob, json_blob, original_proof=proof)


def shared_record_blobs(value, fields, read, previous):
    """Keep only identities the current record names as shared fields."""
    digests = {value[name] for name in fields if name in value}
    return {digest: _read_shared_blob(read, previous, digest) for digest in digests}


def _read_shared_blob(read, shared, digest):
    if digest in shared:
        return shared[digest]
    return read(digest)


def _restore_archive_value(group, value, blob, json_blob, *, original_proof=None):
    restore_blob = partial(_restore_record_blob, blob, original_proof)
    for name in ARCHIVE_BYTES_FIELDS:
        if name in value:
            value[name] = restore_blob(name, value[name])
    archive_record_fields(value, ARCHIVE_JSON_FIELDS, json_blob)
    return restored_archive_record(snapshot_types()[group], value, original_proof=original_proof)


def _restore_record_blob(read, proof, name, digest):
    if name == 'original_content' and proof is not None:
        return proof.content
    return read(digest)


def restored_archive_record(kind, value, *, original_proof=None):
    from compile_receipts import (
        DailySnapshot,
    )

    if kind is DailySnapshot:
        digest = _proven_original_digest(value['original_content'], original_proof)
        require_original_digest({**value, 'original_content': digest})
        value = restore_snapshot_daily(value)
    result = kind(**value)
    require_snapshot_digest(result.content, result.sha256)
    return result


def _decoded_archive_manifest(data):
    value = json.loads(data)
    if not isinstance(value, dict):
        raise progress.ProgressCorrupt('invalid snapshot archive manifest')
    if value.get('schema') == ADMITTED_ARCHIVE_SCHEMA:
        decoded = decoded_snapshot_manifest(data, ADMITTED_ARCHIVE_SCHEMA, {'schema', 'records', 'admission'})
        require_capture_admission(decoded['admission'])
        return decoded
    return decoded_snapshot_manifest(data, ARCHIVE_SCHEMA, {'schema', 'records'})


def decoded_snapshot_manifest(data, schema, expected):
    value = json.loads(data)
    if not isinstance(value, dict) or set(value) != expected or value['schema'] != schema:
        raise progress.ProgressCorrupt('unknown snapshot archive schema')
    _require_archive_record_groups(value['records'])
    if snapshot_json(value) != data:
        raise progress.ProgressCorrupt('invalid snapshot archive manifest')
    return value


def require_archive_members(archive):
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
    progress.require_digest(name.removeprefix('blobs/'))


def _require_archive_record_groups(records):
    if not isinstance(records, dict) or set(records) != set(snapshot_types()):
        raise progress.ProgressCorrupt('invalid snapshot record groups')


def _archive_group_names(group, digests):
    if not isinstance(digests, list):
        raise progress.ProgressCorrupt('snapshot record digests must be a list')
    for index, digest in enumerate(digests):
        progress.require_digest(digest)
        yield archive_record_name(group, index)


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
