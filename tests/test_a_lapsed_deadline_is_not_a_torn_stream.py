"""What the transport calls fatal, and what it merely reports.

Finding K-B17 of the 2026-09-17 audit: the reader thread could end without the
transport becoming fatal, and a deadline that lapsed either side of a complete
write made the whole transport fatal -- one slow caller restarting the server.
Research: docs/research/2026-09-18-sess-a-lapsed-deadline-is-not-a-torn-stream.md
"""

from __future__ import annotations

import threading
import time

import pytest
from lsp_protocol import LspProtocol, ProtocolViolation

from tests.fake_lsp_server import FakeLspServer
from tests.lsp_protocol_helpers import _BlockingReader, _BlockingWriter, _protocol_with_streams
from tests.slow_machine import SHORT_TIMEOUT


@pytest.fixture
def fake_server() -> FakeLspServer:
    server = FakeLspServer()
    yield server
    server.close()


def _await(predicate) -> bool:
    deadline = time.monotonic() + SHORT_TIMEOUT
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_any_reader_failure_makes_the_transport_fatal(
    fake_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    reasons: list[str] = []
    sent = threading.Event()

    def dispatch(_self, _message, *, generation_nonce: str) -> None:
        raise KeyError("a dispatch bug outside the expected error types")

    def handler(peer) -> None:
        peer.send({"jsonrpc": "2.0", "method": "$/progress", "params": {}})
        sent.set()

    monkeypatch.setattr(LspProtocol, "_dispatch_message", dispatch)
    protocol = fake_server.start(handler, fatal_callback=reasons.append)
    assert sent.wait(SHORT_TIMEOUT)

    # `_become_fatal` sets the flag, completes pending work, stops I/O and only
    # then reports the reason, so waiting on the flag can find an empty list -
    # a gap Windows widens past the poll, with a CancelSynchronousIo call that
    # drops the GIL and a socketpair it emulates over loopback TCP. Waiting on
    # the reason still asserts the flag, and a reason that never comes still
    # fails.
    assert (_await(lambda: bool(reasons)), protocol.fatal, reasons) == (
        True,
        True,
        ["failed to read LSP stdout"],
    )


def test_a_write_that_finished_late_is_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    real_write_frame = LspProtocol._write_frame

    def write_then_lapse(current: LspProtocol, task: object) -> None:
        real_write_frame(current, task)
        task.deadline = time.monotonic() - 1.0

    monkeypatch.setattr(LspProtocol, "_write_frame", write_then_lapse)
    try:
        protocol.notify("test/late", {}, deadline=time.monotonic() + SHORT_TIMEOUT)
        assert (_await(lambda: len(writer.frames) == 1), protocol.fatal) == (
            True,
            False,
        )
    finally:
        protocol.close(time.monotonic() + SHORT_TIMEOUT)


def test_a_write_that_never_started_is_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    real_take = LspProtocol._take_task_locked

    def lapse_before_write(current: LspProtocol, task: object):
        taken = real_take(current, task)
        task.deadline = time.monotonic() - 1.0
        return taken

    monkeypatch.setattr(LspProtocol, "_take_task_locked", lapse_before_write)
    try:
        with pytest.raises(ProtocolViolation) as raised:
            protocol.notify(
                "test/never", {}, deadline=time.monotonic() + SHORT_TIMEOUT
            )
        assert (
            isinstance(raised.value.__cause__, TimeoutError),
            writer.frames,
            protocol.fatal,
        ) == (True, [], False)
    finally:
        protocol.close(time.monotonic() + SHORT_TIMEOUT)
