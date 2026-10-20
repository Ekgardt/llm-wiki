"""Canonical entry IDs preserve old text semantics without whole-entry copies."""
import evidence_resolver as resolver
import pytest
from bounded_io import IO_CHUNK_BYTES


class _ObservedSource(bytes):
    def __getitem__(self, key):
        result = super().__getitem__(key)
        if isinstance(key, slice):
            self.slices.append(len(result))
        return result


def _observed(raw):
    value = _ObservedSource(raw)
    value.slices = []
    return value


def _reference_marker(entry):
    for raw in entry.decode('utf-8', errors='replace').splitlines()[1:]:
        line = raw.strip()
        if not line:
            continue
        match = resolver._MARKER_ID_RE.match(line)
        return match.group(1) if match else None
    return None


def _reference_id(content, start, end, declared):
    if declared is not None:
        return declared
    return _reference_marker(content[start:end])


def _reference_entries(content):
    starts = resolver._entry_starts(content)
    ends = [item[0] for item in starts[1:]] + [len(content)]
    entries = [(_reference_id(content, start, end, declared), start, end)
               for (start, declared), end in zip(starts, ends)]
    return [entry for entry in entries if entry[0] is not None]


@pytest.mark.parametrize('header', [
    b'## [heading-id] Heading\n',
    b'<!-- llm-wiki-operation:abc -->\n[12:34:56] Captured\n',
])
def test_identifier_lookup_does_not_copy_complete_entry(header):
    raw = header + b'x' * (IO_CHUNK_BYTES * 3) + b'\n## [last] Heading\n'
    content = _observed(raw)
    assert resolver.daily_entries(content) == _reference_entries(raw)
    assert all(size <= IO_CHUNK_BYTES for size in content.slices)


@pytest.mark.parametrize('transfer', [1, 2, 3, 7, 64])
@pytest.mark.parametrize('suffix', [
    '\n[12:34:56] fact',
    '\r\n \t- \t`[23:59:59]` fact\r\n',
    '\n+   [00:00:00] fact',
    '\n* [01:02:03] fact',
    '\n-[12:34:56] invalid bullet',
    '\n- \n[12:34:56] too late',
    '\nnot a time\n[12:34:56] too late',
    '\n\u00a0\u3000[12:34:56] whitespace',
    '\u2028[12:34:56] on Unicode line inside marker tail\n',
    '\n\n\v\f\x1c\x1d\x1e\x85\u2028\u2029[12:34:56] after blanks',
    '\n[24:00:00] invalid hour',
    '\n[1١:00:00] inherited Unicode digit behavior',
    '\n`[12:34:56] no closing backtick required',
    '\n- \u3000' * 3,
    '',
])
def test_chunked_identifiers_match_previous_parser(monkeypatch, transfer, suffix):
    monkeypatch.setattr(resolver, 'IO_CHUNK_BYTES', transfer, raising=False)
    raw = b'<!-- llm-wiki-operation:abc -->' + suffix.encode()
    raw += b'\n## [tail] Heading\nBody\xff\xe2\x80'
    assert resolver.daily_entries(raw) == _reference_entries(raw)


def test_arbitrarily_long_bullet_whitespace_keeps_its_time():
    raw = (b'<!-- llm-wiki-operation:abc -->\n- '
           + '\u3000'.encode() * IO_CHUNK_BYTES + b'[12:34:56] payload')
    content = _observed(raw)
    assert resolver.daily_entries(content) == [('12:34:56', 0, len(raw))]
    assert all(size <= IO_CHUNK_BYTES for size in content.slices)


@pytest.mark.parametrize('tail', [b'\xff', b'\xe2', b'\xe2\x80', b'\xf0\x9f\x92'])
def test_malformed_utf8_preserves_replacement_semantics(monkeypatch, tail):
    monkeypatch.setattr(resolver, 'IO_CHUNK_BYTES', 1, raising=False)
    raw = b'<!-- llm-wiki-operation:a -->\n' + tail + b'\n[12:34:56] too late'
    assert resolver.daily_entries(raw) == _reference_entries(raw)


class _BulkText(str):
    def __iter__(self):
        raise AssertionError('discarded text scanned character by character')


def test_discarded_header_and_whitespace_use_bulk_text_operations(monkeypatch):
    chunks = [
        _BulkText('<!-- llm-wiki-operation:a -->' + 'x' * IO_CHUNK_BYTES),
        _BulkText('\n' + ' ' * IO_CHUNK_BYTES),
        _BulkText('- ' + '\u3000' * IO_CHUNK_BYTES),
        '[12:34:56] fact',
    ]
    monkeypatch.setattr(resolver, '_decoded_entry_range', lambda *args: iter(chunks))
    assert resolver._marker_entry_id(b'', 0, 0) == '12:34:56'
