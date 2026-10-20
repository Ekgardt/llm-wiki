"""The canonical boundary grammar must work without a whole-file buffer."""
import itertools
import re

import evidence_resolver as resolver
import pytest
from bounded_io import IO_CHUNK_BYTES


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


def _previous_starts(content):
    headings = re.finditer(rb"(?m)^## \[([^\]\r\n]+)\][^\r\n]*(?:\r?\n|$)", content)
    result = [(match.start(), _previous_heading_id(match[1])) for match in headings]
    markers = re.finditer(rb"(?m)^<!-- llm-wiki-operation:[0-9a-f]+ -->[^\r\n]*(?:\r?\n|$)", content)
    return sorted(result + [(match.start(), None) for match in markers])


def _previous_heading_id(raw):
    try:
        block_id = raw.decode('utf-8', errors='strict')
    except UnicodeDecodeError as error:
        raise resolver.EvidenceResolutionError('daily block ID is not UTF-8') from error
    if resolver._BLOCK_ID_RE.fullmatch(block_id) is None:
        raise resolver.EvidenceResolutionError('daily block ID is invalid')
    return block_id


def _outcome(parser, content):
    try:
        return parser(content)
    except resolver.EvidenceResolutionError as error:
        return type(error), str(error)


_HEADERS = (
    b'## [id]', b'## [12:34:56] Heading', b'## [id] suffix\rbroken',
    b'## []', b'## [missing', b'## [bad id]', b'## [\xff]',
    b'## [id] ] more', b'## [a[b]', b'## [id\nsplit]',
    b'<!-- llm-wiki-operation:abc -->',
    b'<!-- llm-wiki-operation:0 -->tail',
    b'<!-- llm-wiki-operation: -->',
    b'<!-- llm-wiki-operation:ABC -->',
    b'<!-- llm-wiki-operation:abc  -->',
    b'<!-- llm-wiki-operation:abc -->tail\rbroken',
    b'<!-- llm-wiki-operation:abc --> -->',
)


@pytest.mark.parametrize('prefix,header,ending', itertools.product(
    (b'', b'\n', b'x ', b'\r'), _HEADERS,
    (b'', b'\n', b'\r\n', b'\r', b'\r\r\n', '\u2028'.encode()),
))
def test_range_boundaries_preserve_previous_regex_grammar(prefix, header, ending):
    raw = prefix + header + ending
    assert _outcome(resolver._entry_starts, _RangeSource(raw)) == _outcome(_previous_starts, raw)


def test_mixed_entries_keep_exact_spans_without_a_buffer():
    raw = (b'preamble\n## [first] Heading\nBody\n'
           b'<!-- llm-wiki-operation:abc -->\n- `[12:34:56]` fact\n'
           b'## [last] End')
    reader = _RangeSource(raw)
    assert resolver._entry_starts(reader) == _previous_starts(raw)
    assert resolver.daily_entries(reader) == resolver.daily_entries(raw)
    assert resolver._daily_part_bounds(reader) == resolver._daily_part_bounds(raw)


@pytest.mark.parametrize('header', [
    b'## [id] ' + b'x' * (IO_CHUNK_BYTES * 3),
    b'<!-- llm-wiki-operation:' + b'a' * (IO_CHUNK_BYTES * 3) + b' -->',
])
def test_long_header_body_and_marker_identifier_are_scanned_in_ranges(header):
    raw = header + b'\n[12:34:56] fact\n## [tail] End\n'
    assert resolver._entry_starts(_RangeSource(raw)) == _previous_starts(raw)
    assert resolver.daily_entries(_RangeSource(raw)) == resolver.daily_entries(raw)


@pytest.mark.parametrize('tail,message', [
    (b'', 'daily block ID is invalid'),
    (b'\xff', 'daily block ID is not UTF-8'),
])
def test_invalid_long_heading_id_is_validated_without_materializing_it(tail, message):
    raw = b'## [' + b'x' * (IO_CHUNK_BYTES * 3) + tail + b']\n'
    with pytest.raises(resolver.EvidenceResolutionError, match=message):
        resolver._entry_starts(_RangeSource(raw))


@pytest.mark.parametrize('transfer', [1, 2, 3])
@pytest.mark.parametrize('identifier', [
    b'id', b'x' * 200, b'x' * 201, 'aя'.encode(), b'a\xe2\x80', b'bad id\xff',
])
def test_heading_id_validation_keeps_utf8_and_grammar_across_chunks(monkeypatch, transfer, identifier):
    monkeypatch.setattr(resolver, 'IO_CHUNK_BYTES', transfer)
    raw = b'## [' + identifier + b']\n'
    assert _outcome(resolver._entry_starts, _RangeSource(raw)) == _outcome(_previous_starts, raw)
