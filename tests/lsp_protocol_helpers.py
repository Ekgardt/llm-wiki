"""Helpers shared by test_lsp_protocol.py and the test files importing them."""
from __future__ import annotations

import threading

from lsp_protocol import LspProtocol

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
