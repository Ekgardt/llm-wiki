"""Retained source originals stay verified and owned while batches use them."""
import gc
import weakref
from dataclasses import replace

import compile_controller as controller
import compile_memory as compiler
import pytest
from bounded_io import VerifiedByteRange

from tests.test_compile_streamed_archive_batches import _archive


def _inputs():
    raw = b'## [12:34:56] event\n' + b'Fact stays complete.\n' * 4000
    parts = tuple(compiler._daily_parts('knowledge/daily/2026-10-20.md', raw))
    assert len(parts) > 1
    return compiler.CompileInputs(parts, (compiler.SourceSnapshot(parts[0].logical_path, raw,
                                                                compiler.sha256_bytes(raw)),), ())


def test_source_record_range_preserves_complete_archive_identity():
    inputs = _inputs()
    expected = compiler.CompileInputs((), inputs.sources, ())
    with _archive(inputs) as reader:
        original = next(reader.daily_records()).original_content
        source = replace(inputs.sources[0], content=original)
        ranged = compiler.CompileInputs((), (source,), ())
        assert controller.compile_inputs_digest(ranged) == controller.compile_inputs_digest(expected)
        with _archive(ranged) as restored:
            assert restored.materialize() == expected


def test_source_record_range_rejects_wrong_declared_digest():
    with _archive(_inputs()) as reader:
        original = next(reader.daily_records()).original_content
        source = compiler.SourceSnapshot('knowledge/daily/test.md', original, '0' * 64)
        with pytest.raises(ValueError, match='digest mismatch'):
            controller._archive_record_value(source, controller._blob_digest)


def test_source_record_range_rejects_closed_original():
    with _archive(_inputs()) as reader:
        original = next(reader.daily_records()).original_content
        source = compiler.SourceSnapshot('knowledge/daily/test.md', original, original.digest)
        reader.close()
        with pytest.raises(ValueError, match='closed'):
            controller._archive_record_value(source, controller._blob_digest)


def test_actual_archive_batches_do_not_read_whole_originals(monkeypatch):
    inputs = _inputs()
    expected = compiler.pack_compile_batches(inputs, model=None)
    with _archive(inputs) as reader:
        read = reader.archive.read

        def bounded_read(name, *args, **kwargs):
            assert name != 'blobs/' + inputs.dailies[0].original_sha256, 'whole original read'
            return read(name, *args, **kwargs)

        monkeypatch.setattr(reader.archive, 'read', bounded_read)
        batches = tuple(reader.iter_batches(model=None))
        assert [batch.manifest for batch in batches] == [batch.manifest for batch in expected]
        _require_range_parts(batches)
        assert _source_records(batches) == _source_records(expected)


def test_part_survives_day_lookahead_but_not_reader_close():
    first = _inputs()
    second = tuple(replace(part, logical_path='knowledge/daily/2026-10-21.md') for part in first.dailies)
    with _archive(replace(first, dailies=first.dailies + second)) as reader:
        records = reader.daily_records()
        part = next(records)
        for _ in range(len(first.dailies)):
            following = next(records)
        assert following.logical_path != part.logical_path
        original = part.original_content
        assert original[part.byte_start:part.byte_end] == part.content
        reader.close()
        with pytest.raises(ValueError, match='closed'):
            original[:1]


def test_finished_day_ranges_are_released_without_accumulating_in_reader():
    inputs = _inputs()
    with _archive(inputs) as reader:
        records = list(reader.daily_records())
        original = records[0].original_content
        assert type(original) is VerifiedByteRange
        stream = original.stream
        reference = weakref.ref(original)
        del records, original
        gc.collect()
        assert reference() is None
        assert stream.closed


def _require_range_parts(batches):
    for batch in batches:
        assert all(type(part.original_content) is VerifiedByteRange for part in batch.inputs.dailies)


def _source_records(batches):
    return [controller._preflight_source_records(batch.inputs) for batch in batches]


def test_single_part_digest_alias_keeps_part_bytes_and_original_range_separate():
    raw = b'## [12:34:56] event\nOne complete fact.\n'
    parts = tuple(compiler._daily_parts('knowledge/daily/2026-10-20.md', raw))
    assert parts[0].sha256 == parts[0].original_sha256
    with _archive(compiler.CompileInputs(parts, (), ())) as reader:
        restored = reader.record('dailies', 0)
        assert type(restored.content) is bytes
        assert restored.content == raw
        assert type(restored.original_content) is VerifiedByteRange
        assert restored.original_content[:] == raw


@pytest.mark.parametrize('prefix', [True, False])
def test_receipt_prefix_proof_uses_verified_ranges_and_owner_scope(tmp_path, prefix):
    from types import SimpleNamespace

    inputs = _inputs()
    selector = compiler._ContextReceiptSelector(SimpleNamespace(vault=tmp_path), float('inf'))
    original = inputs.dailies[0]
    size = original.byte_end if prefix else len(original.original_content)
    source = {'original_byte_size': size, 'original_sha256': compiler.sha256_bytes(original.original_content[:size]),
              'byte_start': original.byte_start, 'byte_end': original.byte_end}
    with _archive(inputs) as reader:
        restored = reader.record('dailies', 0)
        assert selector._context_matches(source, restored)
        assert selector._context_matches(source, restored)
        assert not selector._context_matches(dict(source, original_sha256='0' * 64), restored)
        reader.close()
        with pytest.raises(ValueError, match='closed'):
            selector._context_matches(source, restored)


def test_zip_close_invalidates_retained_original_even_while_member_is_open():
    with _archive(_inputs()) as reader:
        part = reader.record('dailies', 0)
        reader.archive.close()
        with pytest.raises(ValueError, match='closed'):
            part.original_content[:1]
        assert part.original_content.stream.closed


@pytest.mark.parametrize('zip64', [False, True])
def test_range_roundtrip_preserves_complete_canonical_snapshot_identity(monkeypatch, zip64):
    import io
    import zipfile

    if zip64:
        monkeypatch.setattr(zipfile, 'ZIP64_LIMIT', 1024)
    inputs = _inputs()
    with _archive(inputs) as reader:
        restored = replace(inputs, dailies=tuple(reader.daily_records()))
        digest = controller.compile_inputs_digest(inputs)
        assert controller.compile_inputs_digest(restored) == digest
        output = io.BytesIO()
        controller._write_archive_snapshot(restored, digest, output)
        with zipfile.ZipFile(output) as archive:
            copy = controller.CompileInputArchive(archive, digest)
            assert copy.materialize() == inputs
            copy.close()


def test_reader_close_also_closes_explicit_original_range_context():
    inputs = _inputs()
    with _archive(inputs) as reader:
        with reader.original_range(inputs.dailies[0].original_sha256) as original:
            reader.close()
            assert original.stream.closed
            with pytest.raises(ValueError, match='closed'):
                original[:1]


def test_row_lookup_does_not_rehash_every_source_prefix():
    import io

    raw = b''.join(f'Fact number {number:06d} stays complete.\n'.encode() for number in range(8000))

    class Observed(io.BytesIO):
        consumed = 0

        def read(self, size=-1):
            data = super().read(size)
            self.consumed += len(data)
            # Initial proof, newline indexing and monotonically read rows: at
            # most three source passes, independent of the number of rows.
            assert self.consumed <= 3 * len(raw), 'row lookup rescans source prefixes'
            return data

    with Observed(raw) as stream:
        source = VerifiedByteRange(stream, len(raw), compiler.sha256_bytes(raw))
        start = 0
        for row in raw.splitlines(keepends=True):
            assert compiler._line_bounds(source, start, 0) == (start, start + len(row) - 1)
            assert source[start:start + len(row)] == row
            start += len(row)


@pytest.mark.parametrize('ranged', [False, True])
def test_receipt_prefix_cache_cannot_authorize_changed_bytes_with_old_declared_digest(tmp_path, ranged):
    import io
    from types import SimpleNamespace

    inputs = _inputs()
    part = inputs.dailies[0]
    selector = compiler._ContextReceiptSelector(SimpleNamespace(vault=tmp_path), float('inf'))
    source = compiler._v4_source_descriptor(part)
    assert selector._context_matches(source, part)
    changed = part.original_content.replace(b'Fact', b'Fake')
    with io.BytesIO(changed) as stream:
        content = VerifiedByteRange(stream, len(changed), compiler.sha256_bytes(changed)) if ranged else changed
        forged = replace(part, original_content=content)
        assert not selector._context_matches(source, forged)


def test_receipt_source_advance_releases_previous_byte_proof_before_read(tmp_path, monkeypatch):
    from types import SimpleNamespace

    inputs = _inputs()
    selector = compiler._ContextReceiptSelector(SimpleNamespace(vault=tmp_path), float('inf'))
    logical = 'knowledge/daily/2026-10-22.md'
    path = tmp_path / logical
    path.parent.mkdir(parents=True)
    path.write_bytes(b'## [12:34:56] event\nNext source.\n')
    read = compiler._open_daily_source
    with _archive(inputs) as reader:
        part = reader.record('dailies', 0)
        reference = weakref.ref(part.original_content)
        assert selector._context_matches(compiler._v4_source_descriptor(part), part)
        del part

        def observe(target, **kwargs):
            assert reference() is None, 'previous original retained while reading next source'
            return read(target, **kwargs)

        monkeypatch.setattr(compiler, '_open_daily_source', observe)
        with selector._source_parts(logical) as parts:
            assert next(parts)
