"""The canonical boundary grammar must work without a whole-file buffer."""
import itertools

import evidence_resolver as resolver
import pytest
from bounded_io import IO_CHUNK_BYTES

from tests.daily_entry_range_boundaries_helpers import _HEADERS, _outcome, _previous_starts


class _RangeSource:
    def __init__(self, content):
        self.content = content

    def __len__(self):
        return len(self.content)

    def __getitem__(self, key):
        result = self.content[key]
        if isinstance(key, slice):
            assert len(result) <= IO_CHUNK_BYTES, 'whole header/body materialized'
        return result

    def find(self, needle, start=0, end=None):
        return self.content.find(needle, start, end)


@pytest.mark.parametrize('prefix,header,ending', itertools.product(
    (b'', b'\n', b'x ', b'\r'), _HEADERS,
    (b'', b'\n', b'\r\n', b'\r', b'\r\r\n', '\u2028'.encode()),
))
def test_range_boundaries_preserve_previous_regex_grammar(prefix, header, ending):
    raw = prefix + header + ending
    assert _outcome(resolver.entry_starts, _RangeSource(raw)) == _outcome(_previous_starts, raw)


def test_mixed_entries_keep_exact_spans_without_a_buffer():
    raw = (b'preamble\n## [first] Heading\nBody\n'
           b'<!-- llm-wiki-operation:abc -->\n- `[12:34:56]` fact\n'
           b'## [last] End')
    reader = _RangeSource(raw)
    assert resolver.entry_starts(reader) == _previous_starts(raw)
    assert resolver.daily_entries(reader) == resolver.daily_entries(raw)
    assert resolver.daily_part_bounds(reader) == resolver.daily_part_bounds(raw)


@pytest.mark.parametrize('header', [
    b'## [id] ' + b'x' * (IO_CHUNK_BYTES * 3),
    b'<!-- llm-wiki-operation:' + b'a' * (IO_CHUNK_BYTES * 3) + b' -->',
], ids=['long-heading', 'long-operation-marker'])
def test_long_header_body_and_marker_identifier_are_scanned_in_ranges(header):
    raw = header + b'\n[12:34:56] fact\n## [tail] End\n'
    assert resolver.entry_starts(_RangeSource(raw)) == _previous_starts(raw)
    assert resolver.daily_entries(_RangeSource(raw)) == resolver.daily_entries(raw)


@pytest.mark.parametrize('tail,message', [
    (b'', 'daily block ID is invalid'),
    (b'\xff', 'daily block ID is not UTF-8'),
])
def test_invalid_long_heading_id_is_validated_without_materializing_it(tail, message):
    raw = b'## [' + b'x' * (IO_CHUNK_BYTES * 3) + tail + b']\n'
    with pytest.raises(resolver.EvidenceResolutionError, match=message):
        resolver.entry_starts(_RangeSource(raw))


@pytest.mark.parametrize('transfer', [1, 2, 3])
@pytest.mark.parametrize('identifier', [
    b'id', b'x' * 200, b'x' * 201, 'aя'.encode(), b'a\xe2\x80', b'bad id\xff',
])
def test_heading_id_validation_keeps_utf8_and_grammar_across_chunks(monkeypatch, transfer, identifier):
    monkeypatch.setattr(resolver, 'IO_CHUNK_BYTES', transfer)
    raw = b'## [' + identifier + b']\n'
    assert _outcome(resolver.entry_starts, _RangeSource(raw)) == _outcome(_previous_starts, raw)
