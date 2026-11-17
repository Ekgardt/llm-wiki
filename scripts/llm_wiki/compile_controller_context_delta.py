"""The compile context delta: what one batch adds to or removes from its base context."""
from __future__ import annotations

import zipfile
from collections.abc import Sequence

import compile_progress as progress
from llm_wiki.compile_controller_archive import (
    ADMITTED_ARCHIVE_SCHEMA,
    ARCHIVE_SCHEMA,
    CONTEXT_GROUPS,
    SnapshotArchiveWriter,
    blob_digest,
    record_metadata_row,
    snapshot_json,
    write_record_metadata,
)
from llm_wiki.compile_controller_base import require_snapshot_digest, snapshot_types
from llm_wiki.compile_controller_reader import CompileInputArchive, decoded_snapshot_manifest
from llm_wiki.sqlite_deadlines import operational_deadline_active

_CONTEXT_DELTA_SCHEMA = 'compile-context-delta/v2'


def _require_complete_context_base(base):
    if base.manifest['schema'] not in {ARCHIVE_SCHEMA, ADMITTED_ARCHIVE_SCHEMA}:
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
    if type(record) is not snapshot_types()[group]:
        raise ValueError('invalid context record type for group')


class _ContextDeltaWriter:
    def __init__(self, base, archive, binding):
        self.binding = binding
        self.base_digest = base.digest
        self.previous = {group: _context_record_digests(base, group) for group in CONTEXT_GROUPS}
        self.seen = {group: set() for group in CONTEXT_GROUPS}
        self.rows = {group: [] for group in snapshot_types()}
        self.writer = SnapshotArchiveWriter(archive)

    def capture(self, group, record):
        old = self._consume_path(group, record.logical_path)
        _require_context_record_type(group, record)
        require_snapshot_digest(record.content, record.sha256)
        path, data = record_metadata_row(record, blob_digest)
        if blob_digest(data) == old:
            return
        self.rows[group].append(record_metadata_row(record, self.writer.put))

    def _consume_path(self, group, path):
        if group not in self.previous:
            raise ValueError('invalid context delta record group')
        if path in self.seen[group]:
            raise ValueError('duplicate current context record path')
        self.seen[group].add(path)
        return self.previous[group].pop(path, None)

    def finish(self):
        records = {group: write_record_metadata(group, sorted(rows), self.writer.write)
                   for group, rows in self.rows.items()}
        removed = {group: sorted(paths) for group, paths in self.previous.items()}
        manifest = {'schema': _CONTEXT_DELTA_SCHEMA, 'base_digest': self.base_digest,
                    'records': records, 'removed': removed, 'binding': self.binding}
        data = snapshot_json(manifest)
        self.writer.write('manifest.json', data)
        return blob_digest(data)


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
    value = decoded_snapshot_manifest(data, _CONTEXT_DELTA_SCHEMA, fields)
    progress.require_digest(value['base_digest'])
    _require_context_binding(value['binding'])
    _require_removed_context(value['removed'])
    if value['records']['dailies']:
        raise progress.ProgressCorrupt('context delta contains daily records')
    return value


def _require_context_binding(binding):
    if binding is not None:
        progress.require_digest(binding)


def _require_removed_context(removed):
    if not isinstance(removed, dict) or set(removed) != set(CONTEXT_GROUPS):
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
    from llm_wiki.compile_pipeline_sources import (
        CompileInputs,
    )

    _require_complete_context_base(base)
    delta = CompileInputArchive(archive, expected, manifest_decoder=_decoded_context_delta)
    if (delta.manifest['base_digest'], delta.manifest['binding']) != (base.digest, binding):
        raise progress.ProgressConflict('context delta base identity changed')
    groups = {group: _overlay_context_records(base, delta, group) for group in CONTEXT_GROUPS}
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
