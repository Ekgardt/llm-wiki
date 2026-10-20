"""Owned range reads preserve bytes and prove every returned original block."""
import hashlib
import io
import zipfile
from contextlib import contextmanager

import compile_controller as controller
import compile_memory as compiler
import evidence_resolver
import pytest
import reliable_memory
from bounded_io import IO_CHUNK_BYTES

from tests.test_compile_protected_dispatch import _archive_members
from tests.test_compile_streamed_capture_validation import _packed


@contextmanager
def _original(raw, deadline=None):
    digest = hashlib.sha256(raw).hexdigest()
    source = compiler.SourceSnapshot('knowledge/daily/test.md', raw, digest)
    members = _archive_members(compiler.CompileInputs((), (source,), ()))
    expected = hashlib.sha256(members['manifest.json']).hexdigest()
    with zipfile.ZipFile(_packed(members)) as archive:
        reader = controller.CompileInputArchive(archive, expected)
        with reader.original_range(digest, deadline=deadline) as content:
            yield content


@pytest.mark.parametrize('key', [
    0, 1, -1, -2, True, slice(None), slice(1, 5), slice(-8, -1),
    slice(None, None, -1), slice(6, 1, -2), slice(None, None, 3),
    slice(7, 2), slice(2, 7, -1), slice(0, 0), slice(-100, 100),
])
def test_original_range_indexing_matches_bytes(key):
    raw = b'abcdefghi'
    with _original(raw) as content:
        assert content[key] == raw[key]


@pytest.mark.parametrize('needle,start,end', [
    (b'', 0, None), (b'', 100, None), (b'', -100, 2),
    (b'', 4, 2), (b'', 9, None), (b'', 3, -100),
    (b'ab', 0, None), (b'ab', 1, None), (b'ab', -4, None),
    (b'ab', 0, -2), (b'no', 0, None), (b'ab', None, None),
    (b'abcdefghij', 0, None),
])
def test_original_range_search_matches_bytes(needle, start, end):
    raw = b'abcabcaba'
    with _original(raw) as content:
        assert content.find(needle, start, end) == raw.find(needle, start, end)


def test_search_crosses_proven_block_boundaries(monkeypatch):
    monkeypatch.setattr(controller, 'IO_CHUNK_BYTES', 3)
    raw = b'prefix-needle-suffix'
    with _original(raw) as content:
        assert content.find(b'needle') == raw.find(b'needle')
        assert content.find(b'prefix-needle') == 0
        assert content[4:16] == raw[4:16]


def test_empty_original_keeps_its_proven_extent():
    with _original(b'') as content:
        assert len(content) == 0
        assert content[:] == b''
        assert content.find(b'') == 0
        assert list(content.iter_chunks(0, 0)) == []


def test_canonical_parser_and_partitioner_read_verified_archive_ranges():
    raw = (b'## [first] Heading\n' + b'x' * (IO_CHUNK_BYTES * 2)
           + b'\n<!-- llm-wiki-operation:abc -->\n- `[12:34:56]` fact\n')
    with _original(raw) as content:
        assert evidence_resolver.daily_entries(content) == evidence_resolver.daily_entries(raw)
        assert evidence_resolver._daily_part_bounds(content) == evidence_resolver._daily_part_bounds(raw)


class _ObservedStream(io.BytesIO):
    def read(self, size=-1):
        assert 0 < size <= IO_CHUNK_BYTES
        return super().read(min(size, 19))

    def seek(self, offset, whence=0):
        assert (offset, whence) == (0, 0), 'unbounded internal forward seek'
        return super().seek(offset, whence)


def test_short_reads_and_backward_access_stay_bounded():
    raw = b'abcdef' * IO_CHUNK_BYTES
    with _ObservedStream(raw) as stream:
        content = controller._VerifiedArchiveBytes(stream, len(raw), hashlib.sha256(raw).hexdigest(), None)
        assert content[-3:] == raw[-3:]
        assert content[:3] == raw[:3]


def test_reloaded_block_rejects_change_after_initial_verification():
    raw = b'a' * IO_CHUNK_BYTES + b'b' * IO_CHUNK_BYTES
    with io.BytesIO(raw) as stream:
        content = controller._VerifiedArchiveBytes(stream, len(raw), hashlib.sha256(raw).hexdigest(), None)
        stream.seek(IO_CHUNK_BYTES)
        stream.write(b'!')
        stream.seek(0)
        with pytest.raises(ValueError, match='changed after verification'):
            content[IO_CHUNK_BYTES]


@pytest.mark.parametrize('read', [
    lambda content: content[0], lambda content: content[:0],
    lambda content: content.find(b''), lambda content: list(content.iter_chunks(0, 1)),
])
def test_closed_scope_never_returns_even_cached_bytes(read):
    with _original(b'abc') as content:
        assert content[0] == ord('a')
    with pytest.raises(ValueError, match='scope is closed'):
        read(content)


def test_expired_scope_never_returns_cached_bytes(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(reliable_memory.time, 'monotonic', lambda: clock[0])
    with _original(b'abc', deadline=1.0) as content:
        assert content[0] == ord('a')
        clock[0] = 2.0
        with pytest.raises(TimeoutError):
            content[0]


@pytest.mark.parametrize('size', [2, 4])
def test_original_extent_must_match_the_complete_stream(size):
    with io.BytesIO(b'abc') as stream:
        with pytest.raises(ValueError, match='original source'):
            controller._VerifiedArchiveBytes(stream, size, hashlib.sha256(b'abc').hexdigest(), None)
