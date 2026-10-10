"""A live transcript may append; replacement and rewriting remain failures."""
from pathlib import Path

import integration_adapter as adapter
import pytest


def _body() -> bytes:
    return b'first decision\n' + b'middle conversation\n' * 200 + b'last answer\n'


def _change_after_first_read(monkeypatch, change):
    original = adapter.os.read
    changed = False

    def read(descriptor, size):
        nonlocal changed
        result = original(descriptor, size)
        if not changed:
            changed = True
            change()
        return result

    monkeypatch.setattr(adapter.os, 'read', read)


def _append(path: Path):
    with path.open('ab') as stream:
        stream.write(b'next turn\n')


@pytest.mark.parametrize('limit', [256, 8192])
def test_append_during_read_keeps_the_verified_original_content(tmp_path, monkeypatch, limit):
    path = tmp_path / 'session.jsonl'
    body = _body()
    path.write_bytes(body)
    expected = adapter._capture_transcript_text(path, limit)
    _change_after_first_read(monkeypatch, lambda: _append(path))

    assert adapter._capture_transcript_text(path, limit) == expected


@pytest.mark.parametrize('limit', [256, 8192])
def test_replacement_at_the_same_path_is_refused(tmp_path, monkeypatch, limit):
    path = tmp_path / 'session.jsonl'
    path.write_bytes(_body())
    replacement = tmp_path / 'other.jsonl'
    replacement.write_bytes(_body())
    _change_after_first_read(monkeypatch, lambda: replacement.replace(path))

    with pytest.raises((ValueError, PermissionError)):
        adapter._capture_transcript_text(path, limit)


@pytest.mark.parametrize('limit', [256, 8192])
def test_rewrite_followed_by_append_is_refused(tmp_path, monkeypatch, limit):
    path = tmp_path / 'session.jsonl'
    body = _body()
    path.write_bytes(body)
    _change_after_first_read(monkeypatch, lambda: path.write_bytes(b'changed record\n' + body[15:] + b'new turn\n'))

    with pytest.raises((ValueError, PermissionError)):
        adapter._capture_transcript_text(path, limit)


@pytest.mark.parametrize('limit', [256, 8192])
def test_shrink_during_read_is_refused(tmp_path, monkeypatch, limit):
    path = tmp_path / 'session.jsonl'
    path.write_bytes(_body())
    _change_after_first_read(monkeypatch, lambda: path.write_bytes(b'short\n'))

    with pytest.raises((ValueError, PermissionError)):
        adapter._capture_transcript_text(path, limit)


@pytest.mark.parametrize('limit', [256, 8192])
def test_same_size_rewrite_is_refused(tmp_path, monkeypatch, limit):
    path = tmp_path / 'session.jsonl'
    body = _body()
    path.write_bytes(body)
    _change_after_first_read(monkeypatch, lambda: path.write_bytes(b'changed record\n' + body[15:]))

    with pytest.raises((ValueError, PermissionError)):
        adapter._capture_transcript_text(path, limit)


@pytest.mark.parametrize('limit', [256, 8192])
def test_a_symbolic_link_replacing_the_path_is_refused(tmp_path, monkeypatch, limit):
    path = tmp_path / 'session.jsonl'
    path.write_bytes(_body())
    other = tmp_path / 'other.jsonl'
    other.write_bytes(_body())

    def replace():
        path.unlink()
        path.symlink_to(other)

    _change_after_first_read(monkeypatch, replace)
    with pytest.raises((ValueError, PermissionError)):
        adapter._capture_transcript_text(path, limit)


def test_invalid_byte_limits_still_fail(tmp_path):
    path = tmp_path / 'session.jsonl'
    path.write_bytes(_body())
    with pytest.raises(ValueError):
        adapter._capture_transcript_text(path, -1)
