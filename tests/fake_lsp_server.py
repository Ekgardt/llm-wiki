"""In-process hostile LSP peer used only by protocol tests."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
from collections.abc import Callable
from typing import Any, BinaryIO


def _frame(message: object) -> bytes:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def _read_message(stream: BinaryIO) -> dict[str, Any]:
    headers: dict[bytes, bytes] = {}
    while True:
        line = stream.readline()
        if line == b"\r\n":
            break
        name, value = line[:-2].split(b": ", 1)
        headers[name.lower()] = value
    body = stream.read(int(headers[b"content-length"]))
    value = json.loads(body)
    assert isinstance(value, dict)
    return value


class FakeLspPeer:
    """Server side of one socket-backed test connection."""

    def __init__(self, sock: socket.socket) -> None:
        self._socket = sock
        self.reader = sock.makefile("rb")
        self.writer = sock.makefile("wb")
        self._write_lock = threading.Lock()

    def read(self) -> dict[str, Any]:
        return _read_message(self.reader)

    def send(self, message: object) -> None:
        self.send_raw(_frame(message))

    def send_raw(self, value: bytes) -> None:
        with self._write_lock:
            self.writer.write(value)
            self.writer.flush()

    def close(self) -> None:
        try:
            self.writer.close()
        finally:
            try:
                self.reader.close()
            finally:
                self._socket.close()


class FakeLspServer:
    """Own socket pairs and scripted peers for a protocol test."""

    def __init__(self) -> None:
        self.peers: list[FakeLspPeer] = []
        self.protocols: list[object] = []
        self.threads: list[threading.Thread] = []
        self.failures: list[BaseException] = []

    def start(
        self,
        handler: Callable[[FakeLspPeer], None] | None = None,
        *,
        generation_nonce: str = "generation-a",
        fatal_callback: Callable[[str], None] | None = None,
        warning_callback: Callable[[str], None] | None = None,
        server_request_handlers: dict[str, Callable[[object], object]] | None = None,
        server_notification_handlers: dict[str, Callable[[object], None]] | None = None,
    ):
        from lsp_protocol import LspProtocol

        client_socket, server_socket = socket.socketpair()
        client_reader = client_socket.makefile("rb")
        client_writer = client_socket.makefile("wb")
        peer = FakeLspPeer(server_socket)
        protocol = LspProtocol(
            client_reader,
            client_writer,
            generation_nonce,
            fatal_callback=fatal_callback or (lambda _reason: None),
            warning_callback=warning_callback,
            server_request_handlers=server_request_handlers,
            server_notification_handlers=server_notification_handlers,
        )
        protocol._test_socket = client_socket
        self.peers.append(peer)
        self.protocols.append(protocol)
        if handler is not None:
            thread = threading.Thread(
                target=self._run_handler,
                args=(handler, peer),
                daemon=True,
            )
            self.threads.append(thread)
            thread.start()
        return protocol

    def connection(self, scenario: str):
        def handler(peer: FakeLspPeer) -> None:
            request = peer.read()
            request_id = request["id"]
            if scenario == "oversized-frame":
                peer.send_raw(b"Content-Length: 8388609\r\n\r\n")
            elif scenario == "invalid-header":
                peer.send_raw(b"Content-Length 2\r\n\r\n{}")
            elif scenario == "duplicate-content-length":
                peer.send_raw(b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}")
            elif scenario == "wrong-charset":
                peer.send_raw(
                    b"Content-Length: 2\r\n"
                    b"Content-Type: application/vscode-jsonrpc; charset=utf-16\r\n\r\n{}"
                )
            elif scenario == "json-depth-65":
                result: object = None
                for _ in range(64):
                    result = [result]
                peer.send({"jsonrpc": "2.0", "id": request_id, "result": result})
            elif scenario == "batch-message":
                peer.send([{"jsonrpc": "2.0", "id": request_id, "result": None}])
            elif scenario == "duplicate-response-id":
                response = {"jsonrpc": "2.0", "id": request_id, "result": None}
                peer.send_raw(_frame(response) + _frame(response))
            else:
                raise AssertionError(f"unknown scenario: {scenario}")

        return self.start(handler)

    def _run_handler(self, handler: Callable[[FakeLspPeer], None], peer: FakeLspPeer) -> None:
        try:
            handler(peer)
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        except BaseException as exc:  # pragma: no cover - surfaced by close
            self.failures.append(exc)

    def close(self) -> None:
        for protocol in self.protocols:
            protocol.close()
            client_socket = protocol._test_socket
            try:
                client_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client_socket.close()
        for peer in self.peers:
            peer.close()
        for thread in self.threads:
            thread.join(timeout=1)
        if self.failures:
            raise self.failures[0]


def _run_process_server() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stderr-bytes", type=int, default=0)
    parser.add_argument("--report-environment", action="store_true")
    parser.add_argument("--echo", action="store_true")
    parser.add_argument("--exit-while-pending", action="store_true")
    args = parser.parse_args()

    remaining = args.stderr_bytes
    offset = 0
    while remaining:
        size = min(65_537, remaining)
        chunk = bytes((offset + index) % 251 for index in range(size))
        os.write(2, chunk)
        offset += size
        remaining -= size

    if args.report_environment or args.echo or args.exit_while_pending:
        request = _read_message(sys.stdin.buffer)
        if args.exit_while_pending:
            _read_message(sys.stdin.buffer)
            return
        result: object = dict(os.environ) if args.report_environment else request.get("params")
        sys.stdout.buffer.write(
            _frame({"jsonrpc": "2.0", "id": request["id"], "result": result})
        )
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    _run_process_server()
