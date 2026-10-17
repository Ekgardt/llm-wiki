"""A scripted peer must expose unexpected OS failures to the test runner."""

from __future__ import annotations

import errno
import threading
from types import SimpleNamespace

import pytest

from tests.fake_lsp_server import FakeLspServer
from tests.slow_machine import SHORT_TIMEOUT


@pytest.mark.parametrize("error", [
    PermissionError(errno.EPERM, "operation not permitted"),
    PermissionError(errno.EACCES, "access denied"),
    OSError(errno.EIO, "I/O failure"),
    OSError(errno.EBADF, "invalid descriptor"),
    TimeoutError(errno.ETIMEDOUT, "peer deadline elapsed"),
    KeyError("handler defect"),
])
def test_unexpected_handler_error_reaches_teardown(error):
    server = FakeLspServer()

    def handler(_peer):
        raise error

    server._run_handler(handler, SimpleNamespace(close=lambda: None))

    with pytest.raises(type(error)) as raised:
        server.close()
    assert raised.value is error


@pytest.mark.parametrize("error", [
    BrokenPipeError(errno.EPIPE, "peer closed"),
    ConnectionResetError(errno.ECONNRESET, "peer reset"),
    ConnectionAbortedError(errno.ECONNABORTED, "peer aborted"),
])
def test_expected_peer_disconnect_still_allows_teardown(error):
    server = FakeLspServer()

    def handler(_peer):
        raise error

    server._run_handler(handler, None)
    server.close()
    assert server.failures == []


def test_failed_handler_disconnects_before_test_teardown():
    server = FakeLspServer()
    failed = threading.Event()
    error = PermissionError(errno.EPERM, "peer send refused")

    def handler(_peer):
        raise error

    protocol = server.start(handler, fatal_callback=lambda _reason: failed.set())
    try:
        assert failed.wait(SHORT_TIMEOUT), "failed peer left its client waiting for the request deadline"
        assert protocol.fatal
    finally:
        with pytest.raises(PermissionError) as raised:
            server.close()
        assert raised.value is error


def test_successful_handler_does_not_disconnect_an_idle_peer():
    server = FakeLspServer()
    protocol = server.start(lambda _peer: None)
    try:
        server.threads[0].join(SHORT_TIMEOUT)
        assert not server.threads[0].is_alive()
        assert not protocol.fatal
    finally:
        server.close()


def test_peer_cleanup_error_keeps_the_handler_failure_first():
    server = FakeLspServer()
    original = KeyError("handler failed")
    cleanup = OSError("peer close failed")

    def handler(_peer):
        raise original

    def close():
        raise cleanup

    server._run_handler(handler, SimpleNamespace(close=close))
    assert server.failures == [original, cleanup]
    with pytest.raises(KeyError) as raised:
        server.close()
    assert raised.value is original
