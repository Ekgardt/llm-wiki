"""Native journal discovery keeps exact old grammar without retaining bodies."""
import re

import compile_controller as controller
import fact_keys
import pytest
from bounded_io import IO_CHUNK_BYTES

from tests.test_compile_verified_original_ranges import _original

_PREVIOUS = re.compile(
    rb'\n<!-- llm-wiki-operation:[0-9a-f]{64} -->\n## \[[0-9:]{8}\] Captured event\n\n'
    rb'\[Complete captured event\]\(\.\./(raw/sessions/[^)\r\n]+\.breadcrumb\.md)\)\n'
    rb'\[Complete captured event if archived\]\([^\r\n]+\)\n\n(?P<body>    \{[^\r\n]*\}\n)'
)


def _event(body=b'{"text":"hello"}', name=b'event'):
    return (b'\n<!-- llm-wiki-operation:' + b'a' * 64
            + b' -->\n## [12:34:56] Captured event\n\n'
            + b'[Complete captured event](../raw/sessions/' + name + b'.breadcrumb.md)\n'
            + b'[Complete captured event if archived](../archive/location)\n\n    '
            + body + b'\n')


def _previous_index(content):
    return {match.start('body'): ('knowledge/' + match.group(1).decode(), match.group())
            for match in _PREVIOUS.finditer(content)}


class _ObservedRange:
    def __init__(self, content):
        self.content, self.allow_body = content, False

    def __len__(self):
        return len(self.content)

    def find(self, needle, start=0, end=None):
        return self.content.find(needle, start, end)

    def __getitem__(self, key):
        result = self.content[key]
        if isinstance(key, slice) and not self.allow_body:
            assert len(result) <= IO_CHUNK_BYTES, 'native body materialized during discovery'
        return result


def test_range_discovery_reads_bodies_only_when_requested():
    raw = _event(b'{"text":"' + b'x' * (IO_CHUNK_BYTES * 3) + b'"}')
    source = _ObservedRange(raw)
    index = fact_keys._native_journal_index(source)
    assert len(index) == 1
    assert index.heads() == {'knowledge/raw/sessions/event.breadcrumb.md'}
    source.allow_body = True
    assert dict(index.items()) == _previous_index(raw)


@pytest.mark.parametrize('transfer', [7, IO_CHUNK_BYTES])
def test_native_discovery_accepts_verified_archive_ranges(monkeypatch, transfer):
    monkeypatch.setattr(controller, 'IO_CHUNK_BYTES', transfer)
    raw = _event() + _event(name='событие'.encode())
    with _original(raw) as content:
        assert dict(fact_keys._native_journal_index(content).items()) == _previous_index(raw)


def _outcome(content, previous=False):
    try:
        return _previous_index(content) if previous else dict(fact_keys._native_journal_index(content).items())
    except UnicodeDecodeError:
        return 'invalid_utf8'


@pytest.mark.parametrize('raw', [
    b'', _event(), _event()[1:], b'prefix' + _event(), _event() + _event(),
    _event() + _event()[1:], _event().replace(b'\n', b'\r\n'),
    _event().replace(b'a' * 64, b'a' * 63), _event().replace(b'a' * 64, b'a' * 65),
    _event().replace(b'a' * 64, b'A' * 64),
    _event().replace(b'12:34:56', b'99:::999'),
    _event().replace(b'Captured event', b'Captured Event'),
    _event(name=b''), _event(name=b'a)b'), _event(name=b'a(b'), _event(name=b'a\rb'),
    _event(name=b'\xff'), _event(b'bad', name=b'\xff'),
    _event(name='имя'.encode()), _event(b'{}'), _event(b'{not json}'),
    _event(b'{\r}'), _event(b'{\n}'), _event(b'{missing'), _event(b'not a container'),
    _event().replace(b'../archive/location', b''), _event().replace(b'../archive/location', b'a)b'),
    _event().replace(b'../archive/location', b'a\rb'), _event().replace(b'\n\n    ', b'\n    '),
    _event()[:-1], _event(b'bad') + _event(name=b'valid'),
])
def test_native_range_grammar_matches_previous_complete_block_regex(raw):
    assert _outcome(_ObservedRange(raw)) == _outcome(raw, previous=True)


@pytest.mark.parametrize('as_view', [False, True])
def test_mutable_buffer_still_produces_a_stable_index(as_view):
    raw = _event()
    mutable = bytearray(raw)
    source = memoryview(mutable) if as_view else mutable
    index = fact_keys._native_journal_index(source)
    mutable[:] = b'!' * len(mutable)
    assert dict(index.items()) == _previous_index(raw)
