"""Pinned transfer refuses changed responses and respects stream ownership/deadlines."""
import io
from types import SimpleNamespace

import pinned_download as download
import pytest

URL = 'https://example.invalid/pinned'


class Response(io.BytesIO):
    def __init__(self, body=b'payload', *, headers=None, status=200, url=URL):
        super().__init__(body)
        self.headers = {} if headers is None else headers
        self.status = status
        self.url = url
        self.timeouts = []
        self.fp = SimpleNamespace(raw=SimpleNamespace(
            _sock=SimpleNamespace(settimeout=self.timeouts.append),
        ))

    def geturl(self):
        return self.url


def _run(monkeypatch, response, *, deadline=100, limit=100):
    monkeypatch.setattr(download.time, 'monotonic', lambda: 0)
    monkeypatch.setattr(download, 'open_pinned_url', lambda *_args, **_kwargs: response)
    chunks = []
    total = download.download_pinned(URL, chunks.append, deadline=deadline, limit=limit)
    return total, b''.join(chunks)


def test_stream_is_chunked_and_closed_without_declared_length(monkeypatch):
    response = Response(b'abcdefghijk')
    monkeypatch.setattr(download, 'CHUNK_BYTES', 3)
    assert _run(monkeypatch, response) == (11, b'abcdefghijk')
    assert response.closed
    assert len(response.timeouts) == 5
    assert all(0 < value <= 100 for value in response.timeouts)


@pytest.mark.parametrize('options', [
    {'status': 302}, {'url': URL + '/changed'},
    {'headers': {'Content-Length': '101'}},
    {'headers': {'Content-Length': '-1'}},
    {'headers': {'Content-Length': 'not-a-number'}},
    {'headers': {'Content-Length': '99'}},
])
def test_wrong_status_url_or_length_is_refused_and_closed(monkeypatch, options):
    response = Response(**options)
    with pytest.raises(download.PinnedDownloadError):
        _run(monkeypatch, response)
    assert response.closed


def test_stream_without_length_cannot_overflow_caller_limit(monkeypatch):
    response = Response(b'overflow')
    with pytest.raises(download.PinnedDownloadError, match='compressed bound'):
        _run(monkeypatch, response, limit=3)
    assert response.closed


def test_expired_deadline_never_opens_network(monkeypatch):
    monkeypatch.setattr(download.time, 'monotonic', lambda: 10)
    monkeypatch.setattr(download, 'open_pinned_url', lambda *_args, **_kwargs: pytest.fail('opened'))
    with pytest.raises(TimeoutError):
        download.download_pinned(URL, lambda _: None, deadline=9, limit=100)


def test_unbounded_socket_is_refused_before_any_read(monkeypatch):
    response = Response()
    response.fp = None
    with pytest.raises(download.PinnedDownloadError, match='deadline'):
        _run(monkeypatch, response)
    assert response.closed
