"""Recovery verifies complete retained bodies without loading a daily at once."""
import hashlib
import io
import json
import zipfile
from contextlib import contextmanager, nullcontext

import compile_controller as controller
import compile_memory as compiler
import pytest
import reliable_memory
from bounded_io import IO_CHUNK_BYTES

from tests.test_compile_daily_part_lifetime import _long_day
from tests.test_compile_protected_dispatch import _archive_members
from tests.test_compile_transactions import vault as vault


def _packed(members):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return output


def _validate(members, deadline=None):
    digest = hashlib.sha256(members['manifest.json']).hexdigest()
    controller._validate_capture_archive(digest, deadline, _packed(members))


def _inputs(vault):
    return compiler.snapshot_compile_inputs([_long_day(vault[0])])


def test_capture_validation_reads_original_in_bounded_blocks(vault, monkeypatch):
    inputs = _inputs(vault)
    members = _archive_members(inputs)
    original_name = 'blobs/' + inputs.dailies[0].original_sha256
    reads = []
    read = zipfile.ZipExtFile.read

    def observed(handle, size=-1):
        if handle.name == original_name:
            assert 0 < size <= IO_CHUNK_BYTES
            reads.append(size)
        return read(handle, size)

    monkeypatch.setattr(zipfile.ZipExtFile, 'read', observed)
    _validate(members)
    assert len(reads) > 1


def test_capture_validation_does_not_construct_full_records(vault, monkeypatch):
    members = _archive_members(_inputs(vault))

    def forbidden(*args, **kwargs):
        raise AssertionError('validation materialized snapshot records')

    monkeypatch.setattr(controller.CompileInputArchive, 'records', forbidden)
    _validate(members)


def _change_first_record(members, change):
    name = 'records/dailies/0.json'
    value = json.loads(members[name])
    change(value)
    members[name] = controller._snapshot_json(value)
    manifest = json.loads(members['manifest.json'])
    manifest['records']['dailies'][0] = hashlib.sha256(members[name]).hexdigest()
    members['manifest.json'] = controller._snapshot_json(manifest)


@pytest.mark.parametrize('change', [
    lambda value: value.update(sha256='0' * 64),
    lambda value: value.update(original_sha256='0' * 64),
    lambda value: value.pop('logical_path'),
    lambda value: value.update(unrecognized=True),
])
def test_stream_validation_preserves_record_rejection(vault, change):
    members = _archive_members(_inputs(vault))
    _change_first_record(members, change)
    with pytest.raises((ValueError, TypeError, KeyError)):
        _validate(members)


def test_stream_validation_detects_damage_at_end_of_original(vault):
    inputs = _inputs(vault)
    members = _archive_members(inputs)
    name = 'blobs/' + inputs.dailies[0].original_sha256
    members[name] = members[name][:-1] + b'!'
    with pytest.raises(ValueError, match='snapshot content digest mismatch'):
        _validate(members)


def test_validation_rejects_a_self_consistent_part_from_another_original(vault):
    inputs = _inputs(vault)
    members = _archive_members(inputs)
    original = inputs.dailies[0].content
    foreign = b'!' + original[1:]
    digest = hashlib.sha256(foreign).hexdigest()
    members['blobs/' + digest] = foreign
    _change_first_record(members, lambda value: value.update(content=digest, sha256=digest))
    with pytest.raises(ValueError, match='original source range'):
        _validate(members)


@pytest.mark.parametrize('change', [
    lambda value: value.update(byte_start=True),
    lambda value: value.update(byte_start=-1),
    lambda value: value.update(byte_end=value['byte_start'] - 1),
    lambda value: value.update(byte_end=10**9),
])
def test_validation_rejects_invalid_original_range(vault, change):
    members = _archive_members(_inputs(vault))
    _change_first_record(members, change)
    with pytest.raises(ValueError, match='original source range'):
        _validate(members)


@pytest.mark.parametrize('invalid', [False, True])
def test_original_range_is_shared_only_within_validation_and_closed(vault, monkeypatch, invalid):
    members = _archive_members(_inputs(vault))
    if invalid:
        _change_first_record(members, lambda value: value.update(byte_start=-1))
    opened = []
    original_range = controller.CompileInputArchive.original_range

    @contextmanager
    def observed(reader, digest, *, deadline=None):
        with original_range(reader, digest, deadline=deadline) as content:
            opened.append(content)
            yield content

    monkeypatch.setattr(controller.CompileInputArchive, 'original_range', observed)
    expected = pytest.raises(ValueError, match='original source range') if invalid else nullcontext()
    with expected:
        _validate(members)
    assert len(opened) == 1
    assert opened[0].stream.closed


def test_stream_validation_preserves_native_frame_shape_check(vault):
    members = _archive_members(_inputs(vault))
    invalid = controller._snapshot_json([{'unexpected': 'field'}])
    digest = hashlib.sha256(invalid).hexdigest()
    members['blobs/' + digest] = invalid
    _change_first_record(members, lambda value: value.update(native_frames=digest))
    with pytest.raises(TypeError):
        _validate(members)


def test_expired_validation_does_not_open_the_archive(vault, monkeypatch):
    members = _archive_members(_inputs(vault))
    handle = _packed(members)
    digest = hashlib.sha256(members['manifest.json']).hexdigest()

    def forbidden(*args, **kwargs):
        raise AssertionError('expired validation opened archive')

    monkeypatch.setattr(controller.zipfile, 'ZipFile', forbidden)
    with pytest.raises(TimeoutError):
        controller._validate_capture_archive(digest, 0, handle)


def test_validation_deadline_interrupts_original_between_chunks(vault, monkeypatch):
    inputs = _inputs(vault)
    members = _archive_members(inputs)
    original_name = 'blobs/' + inputs.dailies[0].original_sha256
    clock, reads = [0.0], []
    read = zipfile.ZipExtFile.read

    def advance(handle, size=-1):
        result = read(handle, size)
        if handle.name == original_name:
            reads.append(len(result))
            clock[0] = 2.0
        return result

    monkeypatch.setattr(reliable_memory.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(zipfile.ZipExtFile, 'read', advance)
    with pytest.raises(TimeoutError):
        _validate(members, deadline=1.0)
    assert reads == [IO_CHUNK_BYTES]
