"""Entry grammar stays identical when verified blocks are parsed in memory."""
import hashlib
import io

import evidence_resolver as resolver
import pytest
from bounded_io import IO_CHUNK_BYTES, VerifiedByteRange

from tests.test_daily_entry_range_boundaries import _HEADERS, _outcome, _previous_starts


@pytest.mark.parametrize('transfer', [1, 7, 64, IO_CHUNK_BYTES])
@pytest.mark.parametrize('header', _HEADERS)
def test_verified_window_boundaries_preserve_complete_grammar(monkeypatch, transfer, header):
    monkeypatch.setattr(resolver, 'IO_CHUNK_BYTES', transfer)
    raw = b'body\n' + header + b'\r\n## [tail] end\n'
    with io.BytesIO(raw) as stream:
        source = VerifiedByteRange(stream, len(raw), hashlib.sha256(raw).hexdigest())
        assert _outcome(resolver._entry_starts, source) == _outcome(_previous_starts, raw)


def test_many_short_headings_check_owned_blocks_not_every_heading():
    raw = b'## [id] Entry\nBody.\n' * 2000
    checks = []
    with io.BytesIO(raw) as stream:
        source = VerifiedByteRange(stream, len(raw), hashlib.sha256(raw).hexdigest(),
                                   owner_check=lambda: checks.append(True))
        checks.clear()
        expected = _previous_starts(raw)
        assert resolver._entry_starts(source) == expected
        assert len(checks) < len(expected), 'owner checks scale with headings instead of blocks'


def test_long_physical_line_keeps_all_entries_without_whole_line_copy(monkeypatch):
    raw = b'## [first]\n' + b'x' * (IO_CHUNK_BYTES * 3) + b'\n## [last]\n'
    read = VerifiedByteRange.__getitem__

    def bounded_read(source, key):
        result = read(source, key)
        if isinstance(key, slice):
            assert len(result) <= IO_CHUNK_BYTES
        return result

    monkeypatch.setattr(VerifiedByteRange, '__getitem__', bounded_read)
    with io.BytesIO(raw) as stream:
        source = VerifiedByteRange(stream, len(raw), hashlib.sha256(raw).hexdigest())
        assert resolver._entry_starts(source) == _previous_starts(raw)


def test_part_body_is_read_once_for_both_content_and_digest(monkeypatch):
    import compile_memory as compiler

    raw = b'first body\nsecond body\n'
    bounds = ((0, 11), (11, len(raw)))
    reads = []
    read = VerifiedByteRange.__getitem__

    def observe(source, key):
        if isinstance(key, slice):
            reads.append((key.start, key.stop))
        return read(source, key)

    monkeypatch.setattr(VerifiedByteRange, '__getitem__', observe)
    with io.BytesIO(raw) as stream:
        source = VerifiedByteRange(stream, len(raw), hashlib.sha256(raw).hexdigest())
        parts = tuple(compiler._iter_daily_parts('knowledge/daily/test.md', source, bounds=bounds))
        assert tuple(part.content for part in parts) == (raw[:11], raw[11:])
        assert [reads.count(bound) for bound in bounds] == [1, 1]


@pytest.mark.parametrize('raw', [b'', b'\n'])
def test_window_parser_rejects_closed_owner_even_for_empty_source(raw):
    with io.BytesIO(raw) as stream:
        source = VerifiedByteRange(stream, len(raw), hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError, match='closed'):
        resolver._entry_starts(source)
