"""Helpers shared by test_lsp_protocol.py and the test files importing them."""
from __future__ import annotations

import threading
import time

import lsp_protocol
import pytest
from lsp_protocol import LspProtocol, RequestCancelled

from tests.slow_machine import SHORT_TIMEOUT

DRAIN_MARKER = {"jsonrpc": "2.0", "method": "test/drained"}


class _BlockingReader:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.released = threading.Event()
        self.closed = False

    def read(self, _size: int = -1) -> bytes:
        self.started.set()
        self.released.wait(2)
        return b""

    def close(self) -> None:
        self.closed = True
        self.released.set()


class _BlockingWriter:
    def __init__(self, *, block_after: int = 0, fail: bool = False) -> None:
        self.block_after = block_after
        self.fail = fail
        self.frames: list[bytes] = []
        self.started = threading.Event()
        self.released = threading.Event()
        self.closed = False

    def write(self, value: bytes) -> int:
        if len(self.frames) >= self.block_after:
            self.started.set()
            self.released.wait(2)
            if self.fail or self.closed:
                raise OSError("blocked writer stopped")
        self.frames.append(value)
        return len(value)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True
        self.released.set()


def _protocol_with_streams(
    reader: object,
    writer: object,
    *,
    fatal_callback=lambda _reason: None,
    generation_nonce: str = "stream-probe",
) -> LspProtocol:
    return LspProtocol(
        reader,  # type: ignore[arg-type]
        writer,  # type: ignore[arg-type]
        generation_nonce,
        fatal_callback=fatal_callback,
    )


def _protocol_on_blocking_streams(
    block_after: int = 100,
) -> tuple[_BlockingReader, _BlockingWriter, LspProtocol]:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=block_after)
    return reader, writer, _protocol_with_streams(reader, writer)


def _hold_the_writer_on_a_frame(protocol: LspProtocol, writer: _BlockingWriter) -> None:
    protocol._write_message({"jsonrpc": "2.0", "method": "test/block"})
    assert writer.started.wait(SHORT_TIMEOUT)


def _frames_before_drain_marker(protocol: LspProtocol, writer: _BlockingWriter) -> list[bytes]:
    protocol._write_message(DRAIN_MARKER, wait=True)
    assert b"test/drained" in writer.frames[-1]
    return writer.frames[:-1]


def _join_io_threads(protocol: LspProtocol) -> None:
    for thread in (protocol.reader_thread, protocol.writer_thread):
        thread.join(SHORT_TIMEOUT)
        assert not thread.is_alive()


@pytest.fixture(autouse=True, params=[False, True], ids=["posix-close", "windows-close"])
def _close_path(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every case runs through both close paths, not only the one of the host.

    The Windows path (an owner closes its own stream) never ran on Linux, so a
    `fileno()` on a stream its owner had already closed raised ValueError out of
    every close on Windows CI only (run 36327902173). A case that needs one path
    sets the flag itself; its own monkeypatch runs after this one.
    """
    monkeypatch.setattr(lsp_protocol, "CLOSE_WAITS_FOR_A_READ", request.param)

def _request(protocol: LspProtocol, method: str = "initialize", timeout: float = 2) -> object:
    return protocol.request(method, {}, deadline=time.monotonic() + timeout)

class _UninterruptibleReader(_BlockingReader):
    def close(self) -> None:
        self.closed = True

def _await_cancel_frames(writer, count: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while _cancel_frames(writer) < count and time.monotonic() < deadline:
        time.sleep(0.001)

def _await_frames(writer, count: int, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while len(writer.frames) < count and time.monotonic() < deadline:
        time.sleep(0.001)

def _cancel_all_and_expect(sources, futures, *, timeout: float) -> None:
    """Cancel every source; every caller must observe RequestCancelled."""
    for source in sources:
        source.cancel()
    for future in futures:
        with pytest.raises(RequestCancelled):
            future.result(timeout=timeout)

def _cancel_frames(writer) -> int:
    return len([frame for frame in writer.frames if b"$/cancelRequest" in frame])

def _cancellation_threads() -> tuple[threading.Thread, ...]:
    return tuple(
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("lsp-cancel-")
    )

def _dispatch_late_responses(protocol, count: int) -> None:
    for request_id in range(1, count + 1):
        protocol._dispatch_message(
            {"jsonrpc": "2.0", "id": request_id, "result": "late"},
            generation_nonce="stream-probe",
        )
