"""Strict, bounded, hostile-peer tests for the LSP transport."""

from __future__ import annotations

import io
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from lsp_protocol import (
    MAX_DIAGNOSTICS,
    MAX_FRAME_BYTES,
    MAX_HEADER_BYTES,
    MAX_HOVER_BYTES,
    MAX_JSON_DEPTH,
    MAX_LOCATIONS,
    MAX_PENDING_REQUESTS,
    METHOD_NOT_FOUND,
    SERVER_NOTIFICATIONS,
    SERVER_REQUESTS,
    JsonRpcFrameReader,
    JsonRpcResponseError,
    LspProtocol,
    PendingRequestLimitExceeded,
    ProtocolViolation,
    RequestCancelled,
    encode_frame,
    json_depth,
)

from tests.fake_lsp_server import FakeLspPeer, FakeLspServer


@pytest.fixture
def fake_server() -> FakeLspServer:
    server = FakeLspServer()
    yield server
    server.close()


def _body_of_size(size: int) -> bytes:
    prefix = b'{"jsonrpc":"2.0","method":"x","params":{"padding":"'
    suffix = b'"}}'
    assert len(prefix) + len(suffix) <= size
    return prefix + (b"x" * (size - len(prefix) - len(suffix))) + suffix


def _framed(body: bytes, extra_headers: bytes = b"") -> io.BytesIO:
    return io.BytesIO(
        f"Content-Length: {len(body)}\r\n".encode("ascii")
        + extra_headers
        + b"\r\n"
        + body
    )


def _request(protocol: LspProtocol, method: str = "initialize", timeout: float = 2) -> object:
    return protocol.request(method, {}, deadline=time.monotonic() + timeout)


def test_protocol_constants_are_exact() -> None:
    assert MAX_FRAME_BYTES == 8 * 1024 * 1024
    assert MAX_HEADER_BYTES == 8 * 1024
    assert MAX_PENDING_REQUESTS == 32
    assert MAX_LOCATIONS == 10_000
    assert MAX_DIAGNOSTICS == 10_000
    assert MAX_HOVER_BYTES == 256 * 1024
    assert MAX_JSON_DEPTH == 64
    assert METHOD_NOT_FOUND == -32601
    assert SERVER_REQUESTS == frozenset(
        {
            "client/registerCapability",
            "client/unregisterCapability",
            "window/workDoneProgress/create",
            "workspace/configuration",
        }
    )
    assert SERVER_NOTIFICATIONS == frozenset(
        {
            "$/progress",
            "pyright/beginProgress",
            "pyright/endProgress",
            "pyright/reportProgress",
            "textDocument/publishDiagnostics",
        }
    )


def test_frame_reader_accepts_one_strict_lsp_message() -> None:
    body = b'{"jsonrpc":"2.0","id":1,"result":null}'
    stream = io.BytesIO(b"Content-Length: 38\r\n\r\n" + body)
    assert JsonRpcFrameReader(stream).read() == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": None,
    }


def test_encode_frame_is_canonical_utf8_and_byte_counted() -> None:
    frame = encode_frame({"jsonrpc": "2.0", "method": "example", "params": {"x": "β"}})
    header, body = frame.split(b"\r\n\r\n", 1)
    assert header == f"Content-Length: {len(body)}".encode("ascii")
    assert body == b'{"jsonrpc":"2.0","method":"example","params":{"x":"\xce\xb2"}}'


def test_frame_reader_accepts_exactly_eight_mib_and_rejects_one_more() -> None:
    accepted = _body_of_size(MAX_FRAME_BYTES)
    assert JsonRpcFrameReader(_framed(accepted)).read()["method"] == "x"

    rejected = _body_of_size(MAX_FRAME_BYTES + 1)
    with pytest.raises(ProtocolViolation, match="frame"):
        JsonRpcFrameReader(_framed(rejected)).read()


def test_frame_reader_accepts_exactly_eight_kib_header_and_rejects_one_more() -> None:
    body = b'{"jsonrpc":"2.0","method":"x"}'
    base = f"Content-Length: {len(body)}\r\n".encode("ascii")
    exact_padding = MAX_HEADER_BYTES - len(base) - len(b"X-Pad: \r\n") - len(b"\r\n")
    exact = base + b"X-Pad: " + b"x" * exact_padding + b"\r\n\r\n" + body
    assert JsonRpcFrameReader(io.BytesIO(exact)).read()["method"] == "x"

    oversized = base + b"X-Pad: " + b"x" * (exact_padding + 1) + b"\r\n\r\n" + body
    with pytest.raises(ProtocolViolation, match="header"):
        JsonRpcFrameReader(io.BytesIO(oversized)).read()


@pytest.mark.parametrize(
    "stream",
    [
        io.BytesIO(b"Content-Length: 2\n\n{}"),
        io.BytesIO(b"Content-Length 2\r\n\r\n{}"),
        io.BytesIO(b"Content-Length: +2\r\n\r\n{}"),
        io.BytesIO(b"Content-Length: 2 \r\n\r\n{}"),
        io.BytesIO(b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}"),
        io.BytesIO(b"Content-Type: application/vscode-jsonrpc\r\n\r\n{}"),
        io.BytesIO(b"\xff: 2\r\nContent-Length: 2\r\n\r\n{}"),
    ],
)
def test_frame_reader_rejects_invalid_headers(stream: io.BytesIO) -> None:
    with pytest.raises(ProtocolViolation):
        JsonRpcFrameReader(stream).read()


def test_frame_reader_wraps_pathological_content_length_as_protocol_violation() -> None:
    stream = io.BytesIO(b"Content-Length: " + (b"9" * 5000) + b"\r\n\r\n")
    with pytest.raises(ProtocolViolation, match="Content-Length"):
        JsonRpcFrameReader(stream).read()


@pytest.mark.parametrize("charset", [b"utf-16", b"ascii", b"latin-1"])
def test_frame_reader_rejects_wrong_charset(charset: bytes) -> None:
    header = b"Content-Type: application/vscode-jsonrpc; charset=" + charset + b"\r\n"
    with pytest.raises(ProtocolViolation, match="charset"):
        JsonRpcFrameReader(_framed(b"{}", header)).read()


def test_content_type_without_charset_uses_utf8_default() -> None:
    body = b'{"jsonrpc":"2.0","method":"x"}'
    header = b"Content-Type: application/vscode-jsonrpc\r\n"
    assert JsonRpcFrameReader(_framed(body, header)).read()["method"] == "x"


@pytest.mark.parametrize(
    "body",
    [
        b"\xff",
        b"{",
        b'{"jsonrpc":"2.0","method":NaN}',
        b'{"jsonrpc":"2.0","method":"a","method":"b"}',
    ],
)
def test_frame_reader_rejects_malformed_utf8_or_json(body: bytes) -> None:
    with pytest.raises(ProtocolViolation):
        JsonRpcFrameReader(_framed(body)).read()


def test_json_depth_accepts_64_and_rejects_65_without_recursion() -> None:
    params: object = None
    for _ in range(62):
        params = [params]
    depth_64 = {"jsonrpc": "2.0", "method": "x", "params": [params]}
    assert json_depth(depth_64) == 64
    assert JsonRpcFrameReader(_framed(json.dumps(depth_64).encode())).read() == depth_64

    depth_65 = {"jsonrpc": "2.0", "method": "x", "params": [[params]]}
    assert json_depth(depth_65) == 65
    with pytest.raises(ProtocolViolation, match="depth"):
        JsonRpcFrameReader(_framed(json.dumps(depth_65).encode())).read()


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b"null",
        b'{"jsonrpc":"1.0","method":"x"}',
        b'{"jsonrpc":2.0,"method":"x"}',
        b'{"jsonrpc":"2.0"}',
        b'{"jsonrpc":"2.0","id":true,"result":null}',
        b'{"jsonrpc":"2.0","id":1.5,"result":null}',
        b'{"jsonrpc":"2.0","id":null,"result":null}',
        b'{"jsonrpc":"2.0","id":1,"result":null,"error":{"code":-1,"message":"x"}}',
        b'{"jsonrpc":"2.0","id":1}',
        b'{"jsonrpc":"2.0","method":1}',
        b'{"jsonrpc":"2.0","method":"x","params":1}',
    ],
)
def test_frame_reader_rejects_invalid_json_rpc_shapes(body: bytes) -> None:
    with pytest.raises(ProtocolViolation):
        JsonRpcFrameReader(_framed(body)).read()


@pytest.mark.parametrize("request_id", [-(2**31), 2**31 - 1, "", "server-request"])
def test_request_id_edge_values_are_accepted(request_id: int | str) -> None:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": "workspace/configuration"},
        separators=(",", ":"),
    ).encode()
    assert JsonRpcFrameReader(_framed(body)).read()["id"] == request_id


@pytest.mark.parametrize("request_id", [-(2**31) - 1, 2**31, True, 1.5, None])
def test_request_id_values_outside_lsp_contract_are_rejected(request_id: object) -> None:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": "workspace/configuration"},
        separators=(",", ":"),
    ).encode()
    with pytest.raises(ProtocolViolation, match="ID"):
        JsonRpcFrameReader(_framed(body)).read()


@pytest.mark.parametrize(
    "scenario",
    [
        "oversized-frame",
        "invalid-header",
        "duplicate-content-length",
        "wrong-charset",
        "json-depth-65",
        "batch-message",
        "duplicate-response-id",
    ],
)
def test_protocol_violation_is_fatal(scenario: str, fake_server: FakeLspServer) -> None:
    connection = fake_server.connection(scenario)
    with pytest.raises(ProtocolViolation):
        _request(connection)
    assert connection.fatal is True


def test_fatal_callback_runs_once_and_fails_all_pending_once(
    fake_server: FakeLspServer,
) -> None:
    callbacks: list[str] = []
    peer_ready = threading.Event()

    def handler(peer: FakeLspPeer) -> None:
        peer.read()
        peer_ready.set()
        peer.send_raw(b"Bad\r\n\r\n")
        peer.send_raw(b"Worse\r\n\r\n")

    protocol = fake_server.start(handler, fatal_callback=callbacks.append)
    with pytest.raises(ProtocolViolation):
        _request(protocol)
    assert peer_ready.wait(1)
    time.sleep(0.02)
    assert len(callbacks) == 1
    assert callbacks[0]
    with pytest.raises(ProtocolViolation):
        _request(protocol)
    assert len(callbacks) == 1


def test_server_error_is_typed_and_preserves_bounded_shape(fake_server: FakeLspServer) -> None:
    def handler(peer: FakeLspPeer) -> None:
        request = peer.read()
        peer.send(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"code": -32001, "message": "failed", "data": {"safe": True}},
            }
        )

    protocol = fake_server.start(handler)
    with pytest.raises(JsonRpcResponseError) as raised:
        _request(protocol)
    assert raised.value.error.code == -32001
    assert raised.value.error.message == "failed"
    assert raised.value.error.data == {"safe": True}
    assert protocol.fatal is False


def test_exactly_32_requests_can_be_active_and_request_33_is_rejected(
    fake_server: FakeLspServer,
) -> None:
    received: list[dict[str, Any]] = []
    all_received = threading.Event()
    release = threading.Event()

    def handler(peer: FakeLspPeer) -> None:
        for _ in range(MAX_PENDING_REQUESTS):
            received.append(peer.read())
        all_received.set()
        assert release.wait(2)
        for request in reversed(received):
            peer.send({"jsonrpc": "2.0", "id": request["id"], "result": request["id"]})

    protocol = fake_server.start(handler)
    with ThreadPoolExecutor(max_workers=MAX_PENDING_REQUESTS) as pool:
        futures = [pool.submit(_request, protocol, "example", 3) for _ in range(32)]
        assert all_received.wait(2)
        with pytest.raises(PendingRequestLimitExceeded):
            _request(protocol, timeout=1)
        release.set()
        assert sorted(future.result(timeout=2) for future in futures) == list(range(1, 33))
    assert protocol.pending_count == 0


def test_concurrent_writes_remain_whole_frames(fake_server: FakeLspServer) -> None:
    seen: list[int] = []

    def handler(peer: FakeLspPeer) -> None:
        for _ in range(32):
            request = peer.read()
            seen.append(request["id"])
            peer.send({"jsonrpc": "2.0", "id": request["id"], "result": None})

    protocol = fake_server.start(handler)
    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = [pool.submit(_request, protocol) for _ in range(32)]
        assert [future.result(timeout=2) for future in futures] == [None] * 32
    assert sorted(seen) == list(range(1, 33))


def test_cancellation_sends_cancel_and_drops_late_response(fake_server: FakeLspServer) -> None:
    cancelled = threading.Event()
    cancel_seen = threading.Event()

    def handler(peer: FakeLspPeer) -> None:
        request = peer.read()
        cancellation = peer.read()
        assert cancellation == {
            "jsonrpc": "2.0",
            "method": "$/cancelRequest",
            "params": {"id": request["id"]},
        }
        cancel_seen.set()
        peer.send({"jsonrpc": "2.0", "id": request["id"], "result": "late"})
        follow_up = peer.read()
        peer.send({"jsonrpc": "2.0", "id": follow_up["id"], "result": "fresh"})

    protocol = fake_server.start(handler)
    timer = threading.Timer(0.03, cancelled.set)
    timer.start()
    with pytest.raises(RequestCancelled):
        protocol.request(
            "slow", {}, deadline=time.monotonic() + 1, cancelled=cancelled.is_set
        )
    assert cancel_seen.wait(1)
    assert _request(protocol) == "fresh"
    assert protocol.fatal is False
    timer.join()


def test_timeout_cleans_pending_without_waiting_past_original_deadline(
    fake_server: FakeLspServer,
) -> None:
    def handler(peer: FakeLspPeer) -> None:
        peer.read()

    protocol = fake_server.start(handler)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        protocol.request("slow", {}, deadline=started + 0.05)
    assert time.monotonic() - started < 0.25
    assert protocol.pending_count == 0


def test_close_fails_an_active_request_instead_of_returning_a_result(
    fake_server: FakeLspServer,
) -> None:
    request_seen = threading.Event()

    def handler(peer: FakeLspPeer) -> None:
        peer.read()
        request_seen.set()

    protocol = fake_server.start(handler)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_request, protocol, "slow", 2)
        assert request_seen.wait(1)
        protocol.close()
        with pytest.raises(ProtocolViolation, match="closed"):
            future.result(timeout=1)


def test_late_response_from_another_generation_is_dropped(fake_server: FakeLspServer) -> None:
    response_sent = threading.Event()

    def handler(peer: FakeLspPeer) -> None:
        request = peer.read()
        protocol._dispatch_message(
            {"jsonrpc": "2.0", "id": request["id"], "result": "stale"},
            generation_nonce="generation-old",
        )
        response_sent.set()
        peer.send({"jsonrpc": "2.0", "id": request["id"], "result": "current"})

    protocol = fake_server.start(handler, generation_nonce="generation-current")
    assert _request(protocol) == "current"
    assert response_sent.wait(1)
    assert protocol.fatal is False


def test_pending_identity_contains_generation_and_request_id(fake_server: FakeLspServer) -> None:
    observed: list[tuple[str, int]] = []

    def handler(peer: FakeLspPeer) -> None:
        request = peer.read()
        observed.extend(protocol.pending_keys)
        peer.send({"jsonrpc": "2.0", "id": request["id"], "result": None})

    protocol = fake_server.start(handler, generation_nonce="nonce-123")
    assert _request(protocol) is None
    assert observed == [("nonce-123", 1)]


@pytest.mark.parametrize(
    "method",
    [
        "textDocument/definition",
        "textDocument/declaration",
        "textDocument/typeDefinition",
        "textDocument/implementation",
        "textDocument/references",
        "textDocument/documentSymbol",
        "workspace/symbol",
    ],
)
def test_location_result_ceiling_accepts_10000_and_rejects_10001(
    method: str, fake_server: FakeLspServer
) -> None:
    def handler(peer: FakeLspPeer) -> None:
        first = peer.read()
        peer.send({"jsonrpc": "2.0", "id": first["id"], "result": [None] * MAX_LOCATIONS})
        second = peer.read()
        peer.send(
            {"jsonrpc": "2.0", "id": second["id"], "result": [None] * (MAX_LOCATIONS + 1)}
        )

    protocol = fake_server.start(handler)
    assert len(protocol.request(method, {}, deadline=time.monotonic() + 2)) == MAX_LOCATIONS
    with pytest.raises(ProtocolViolation, match="location"):
        protocol.request(method, {}, deadline=time.monotonic() + 2)
    assert protocol.fatal is True


def test_diagnostic_ceiling_accepts_10000_and_rejects_10001(
    fake_server: FakeLspServer,
) -> None:
    received: list[object] = []
    first_sent = threading.Event()

    def handler(peer: FakeLspPeer) -> None:
        peer.send(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": "file:///x.py", "diagnostics": [None] * MAX_DIAGNOSTICS},
            }
        )
        first_sent.set()
        peer.read()
        peer.send(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {
                    "uri": "file:///x.py",
                    "diagnostics": [None] * (MAX_DIAGNOSTICS + 1),
                },
            }
        )

    protocol = fake_server.start(
        handler,
        server_notification_handlers={"textDocument/publishDiagnostics": received.append},
    )
    assert first_sent.wait(1)
    deadline = time.monotonic() + 1
    while not received and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(received[0]["diagnostics"]) == MAX_DIAGNOSTICS
    with pytest.raises(ProtocolViolation, match="diagnostic"):
        _request(protocol)


def _hover_with_encoded_size(size: int) -> dict[str, str]:
    empty = {"contents": ""}
    overhead = len(json.dumps(empty, ensure_ascii=False, separators=(",", ":")).encode())
    return {"contents": "x" * (size - overhead)}


def test_hover_ceiling_accepts_256_kib_and_rejects_one_more(fake_server: FakeLspServer) -> None:
    def handler(peer: FakeLspPeer) -> None:
        for size in (MAX_HOVER_BYTES, MAX_HOVER_BYTES + 1):
            request = peer.read()
            peer.send(
                {"jsonrpc": "2.0", "id": request["id"], "result": _hover_with_encoded_size(size)}
            )

    protocol = fake_server.start(handler)
    result = protocol.request("textDocument/hover", {}, deadline=time.monotonic() + 2)
    assert len(json.dumps(result, separators=(",", ":")).encode()) == MAX_HOVER_BYTES
    with pytest.raises(ProtocolViolation, match="hover"):
        protocol.request("textDocument/hover", {}, deadline=time.monotonic() + 2)


def test_all_allowlisted_server_requests_execute_registered_handlers(
    fake_server: FakeLspServer,
) -> None:
    calls: list[tuple[str, object]] = []
    completed = threading.Event()

    def handler(peer: FakeLspPeer) -> None:
        for index, method in enumerate(sorted(SERVER_REQUESTS), start=100):
            peer.send({"jsonrpc": "2.0", "id": index, "method": method, "params": {"x": 1}})
            response = peer.read()
            assert response == {"jsonrpc": "2.0", "id": index, "result": method}
        completed.set()

    handlers = {
        method: (lambda params, method=method: calls.append((method, params)) or method)
        for method in SERVER_REQUESTS
    }
    protocol = fake_server.start(handler, server_request_handlers=handlers)
    assert completed.wait(2)
    assert {method for method, _params in calls} == SERVER_REQUESTS
    assert protocol.fatal is False


@pytest.mark.parametrize(
    "method",
    ["unknown/request", "workspace/applyEdit", "workspace/executeCommand", "window/showDocument"],
)
def test_unknown_and_mutating_server_requests_receive_method_not_found(
    method: str, fake_server: FakeLspServer
) -> None:
    completed = threading.Event()

    def handler(peer: FakeLspPeer) -> None:
        peer.send({"jsonrpc": "2.0", "id": "server-id", "method": method, "params": {}})
        response = peer.read()
        assert response["id"] == "server-id"
        assert response["error"]["code"] == METHOD_NOT_FOUND
        assert response["error"]["message"] == "Method not found"
        completed.set()

    protocol = fake_server.start(handler)
    assert completed.wait(1)
    assert protocol.fatal is False


def test_all_allowlisted_notifications_execute_registered_handlers(
    fake_server: FakeLspServer,
) -> None:
    calls: list[str] = []
    completed = threading.Event()

    def handler(peer: FakeLspPeer) -> None:
        for method in sorted(SERVER_NOTIFICATIONS):
            params = {"diagnostics": []} if method == "textDocument/publishDiagnostics" else {}
            peer.send({"jsonrpc": "2.0", "method": method, "params": params})
        completed.set()

    handlers = {
        method: (lambda _params, method=method: calls.append(method))
        for method in SERVER_NOTIFICATIONS
    }
    protocol = fake_server.start(handler, server_notification_handlers=handlers)
    assert completed.wait(1)
    deadline = time.monotonic() + 1
    while len(calls) < len(SERVER_NOTIFICATIONS) and time.monotonic() < deadline:
        time.sleep(0.005)
    assert set(calls) == SERVER_NOTIFICATIONS
    assert protocol.fatal is False


def test_unknown_notifications_are_dropped_with_one_bounded_stable_warning(
    fake_server: FakeLspServer,
) -> None:
    warnings: list[str] = []
    completed = threading.Event()
    hostile_method = "unknown/" + "secret" * 10_000

    def handler(peer: FakeLspPeer) -> None:
        for method in (hostile_method, "another/unknown"):
            peer.send({"jsonrpc": "2.0", "method": method, "params": {"secret": "value"}})
        completed.set()

    protocol = fake_server.start(handler, warning_callback=warnings.append)
    assert completed.wait(1)
    deadline = time.monotonic() + 1
    while not warnings and time.monotonic() < deadline:
        time.sleep(0.005)
    assert warnings == ["dropped unknown server notification"]
    assert len(warnings[0].encode()) <= 128
    assert "secret" not in warnings[0]
    assert protocol.fatal is False


def test_one_reader_thread_owns_stdout(fake_server: FakeLspServer) -> None:
    protocol = fake_server.start()
    assert protocol.reader_thread.is_alive()
    assert protocol.reader_thread.name == "lsp-stdout-generation-a"
    assert protocol.stdout_reader_owner == protocol.reader_thread.ident
    assert protocol.writer_thread.is_alive()
    assert protocol.writer_thread.name == "lsp-stdin-generation-a"
    assert protocol.stdin_writer_owner == protocol.writer_thread.ident


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
) -> LspProtocol:
    return LspProtocol(
        reader,  # type: ignore[arg-type]
        writer,  # type: ignore[arg-type]
        "stream-probe",
        fatal_callback=fatal_callback,
    )


def test_response_winning_cancellation_race_removes_pending_without_tombstone_leak() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    callback_calls = 0

    def cancelled() -> bool:
        nonlocal callback_calls
        callback_calls += 1
        protocol._dispatch_message(
            {"jsonrpc": "2.0", "id": 1, "result": "response-won"},
            generation_nonce="stream-probe",
        )
        return True

    assert protocol.request(
        "race", {}, deadline=time.monotonic() + 1, cancelled=cancelled
    ) == "response-won"
    assert callback_calls == 1
    assert protocol.pending_count == 0
    assert protocol.pending_keys == ()
    protocol.close()


def test_fatal_dispatch_stops_before_later_notification_handler(
    fake_server: FakeLspServer,
) -> None:
    handled: list[object] = []
    sent = threading.Event()

    def handler(peer: FakeLspPeer) -> None:
        peer.send_raw(
            _raw_frame(
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/publishDiagnostics",
                    "params": {"diagnostics": [None] * (MAX_DIAGNOSTICS + 1)},
                }
            )
            + _raw_frame({"jsonrpc": "2.0", "method": "$/progress", "params": {}})
        )
        sent.set()

    protocol = fake_server.start(
        handler,
        server_notification_handlers={"$/progress": handled.append},
    )
    assert sent.wait(1)
    deadline = time.monotonic() + 1
    while not protocol.fatal and time.monotonic() < deadline:
        time.sleep(0.005)
    time.sleep(0.02)
    assert protocol.fatal is True
    assert handled == []
    assert not protocol.reader_thread.is_alive()
    assert not protocol.writer_thread.is_alive()


def _raw_frame(message: object) -> bytes:
    body = json.dumps(message, separators=(",", ":")).encode()
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


def test_initial_blocked_write_is_bounded_by_request_deadline_and_fatal() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter()
    callbacks: list[str] = []
    protocol = _protocol_with_streams(reader, writer, fatal_callback=callbacks.append)
    deadline = time.monotonic() + 0.05

    with pytest.raises((TimeoutError, ProtocolViolation)):
        protocol.request("blocked", {}, deadline=deadline)

    assert time.monotonic() < deadline + 0.2
    assert writer.started.wait(1)
    assert protocol.fatal is True
    assert protocol.pending_count == 0
    assert len(callbacks) == 1
    assert protocol.writer_thread is not threading.current_thread()
    writer.released.set()
    protocol.close()


def test_blocked_cancellation_write_never_extends_original_deadline() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=1)
    protocol = _protocol_with_streams(reader, writer)
    cancel = threading.Event()
    timer = threading.Timer(0.02, cancel.set)
    deadline = time.monotonic() + 0.08
    timer.start()

    with pytest.raises(RequestCancelled):
        protocol.request("cancel", {}, deadline=deadline, cancelled=cancel.is_set)

    assert time.monotonic() < deadline + 0.2
    assert writer.started.wait(1)
    assert protocol.pending_count == 0
    assert protocol.fatal is True
    writer.released.set()
    timer.join()
    protocol.close()


def test_writer_failure_is_fatal_and_cleans_pending_once() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(fail=True)
    callbacks: list[str] = []
    protocol = _protocol_with_streams(reader, writer, fatal_callback=callbacks.append)
    writer.released.set()

    with pytest.raises(ProtocolViolation):
        protocol.request("write-failure", {}, deadline=time.monotonic() + 1)

    assert protocol.pending_count == 0
    deadline = time.monotonic() + 1
    while not callbacks and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(callbacks) == 1
    protocol.close()


def test_close_unblocks_and_joins_socket_reader_and_writer_owners(
    fake_server: FakeLspServer,
) -> None:
    protocol = fake_server.start()
    assert protocol.reader_thread.is_alive()
    protocol.close()
    assert not protocol.reader_thread.is_alive()
    assert not protocol.writer_thread.is_alive()


def test_close_unblocks_native_pipe_reader_and_stops_owners() -> None:
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb", buffering=0)
    peer_writer = os.fdopen(write_fd, "wb", buffering=0)
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    try:
        protocol.close()
        assert not protocol.reader_thread.is_alive()
        assert not protocol.writer_thread.is_alive()
    finally:
        peer_writer.close()


def test_native_pipe_write_is_bounded_by_deadline_on_every_platform() -> None:
    peer_read_fd, write_fd = os.pipe()
    peer_reader = os.fdopen(peer_read_fd, "rb", buffering=0)
    writer = os.fdopen(write_fd, "wb", buffering=0)
    reader = _BlockingReader()
    protocol = _protocol_with_streams(reader, writer)
    deadline = time.monotonic() + 0.1
    try:
        with pytest.raises((TimeoutError, ProtocolViolation)):
            protocol.request(
                "pipe-block",
                {"payload": "x" * (1024 * 1024)},
                deadline=deadline,
            )
        assert time.monotonic() < deadline + 0.3
        assert protocol.fatal is True
        assert protocol.pending_count == 0
    finally:
        protocol.close()
        peer_reader.close()


def test_close_during_blocked_initial_write_cleans_request_and_owners() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter()
    protocol = _protocol_with_streams(reader, writer)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            protocol.request,
            "close-race",
            {},
            deadline=time.monotonic() + 2,
        )
        assert writer.started.wait(1)
        protocol.close()
        with pytest.raises(ProtocolViolation, match="closed"):
            future.result(timeout=1)
    assert protocol.pending_count == 0
    assert not protocol.reader_thread.is_alive()
    assert not protocol.writer_thread.is_alive()


def test_close_from_reader_handler_does_not_deadlock(fake_server: FakeLspServer) -> None:
    handler_returned = threading.Event()

    def peer_handler(peer: FakeLspPeer) -> None:
        peer.send({"jsonrpc": "2.0", "method": "$/progress", "params": {}})

    def notification_handler(_params: object) -> None:
        protocol.close()
        handler_returned.set()

    protocol = fake_server.start(
        peer_handler,
        server_notification_handlers={"$/progress": notification_handler},
    )
    assert handler_returned.wait(1)
    protocol.reader_thread.join(1)
    assert not protocol.reader_thread.is_alive()
    assert not protocol.writer_thread.is_alive()


def test_encode_frame_rejects_cycles_quickly() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    started = time.monotonic()
    with pytest.raises(ProtocolViolation, match="cyclic"):
        encode_frame({"jsonrpc": "2.0", "method": "x", "params": cyclic})
    assert time.monotonic() - started < 0.2


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_encode_frame_rejects_non_finite_numbers_in_values_and_keys(number: float) -> None:
    for params in ({"number": number}, {number: "value"}):
        with pytest.raises(ProtocolViolation, match="finite"):
            encode_frame({"jsonrpc": "2.0", "method": "x", "params": params})


@pytest.mark.parametrize("location", ["value", "key"])
def test_encode_frame_rejects_lone_surrogates_everywhere(location: str) -> None:
    params = {"value": "\ud800"} if location == "value" else {"\udfff": "value"}
    with pytest.raises(ProtocolViolation, match="surrogate"):
        encode_frame({"jsonrpc": "2.0", "method": "x", "params": params})


@pytest.mark.parametrize(
    "body",
    [
        b'{"jsonrpc":"2.0","method":"x","params":{"number":1e999}}',
        b'{"jsonrpc":"2.0","method":"x","params":{"value":"\\ud800"}}',
        b'{"jsonrpc":"2.0","method":"x","params":{"\\udfff":"value"}}',
    ],
)
def test_frame_reader_rejects_non_finite_numbers_and_lone_surrogates(body: bytes) -> None:
    with pytest.raises(ProtocolViolation):
        JsonRpcFrameReader(_framed(body)).read()


def test_frame_reader_accepts_a_valid_escaped_surrogate_pair() -> None:
    body = b'{"jsonrpc":"2.0","method":"x","params":{"value":"\\ud83d\\ude00"}}'
    message = JsonRpcFrameReader(_framed(body)).read()
    assert message["params"]["value"] == "😀"


def test_cancelled_callback_exception_cleans_pending_sends_one_cancel_and_propagates() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)

    def cancelled() -> bool:
        raise LookupError("callback failed")

    with pytest.raises(LookupError, match="callback failed"):
        protocol.request(
            "callback-error",
            {},
            deadline=time.monotonic() + 1,
            cancelled=cancelled,
        )

    deadline = time.monotonic() + 1
    while len(writer.frames) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert protocol.pending_count == 0
    assert len(writer.frames) == 2
    assert b'"method":"$/cancelRequest"' in writer.frames[1]
    protocol.close()


def test_cancelled_callback_exception_remains_bounded_when_cancel_write_blocks() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=1)
    protocol = _protocol_with_streams(reader, writer)

    def cancelled() -> bool:
        raise LookupError("callback failed during blocked cancel")

    deadline = time.monotonic() + 0.08
    with pytest.raises(LookupError, match="blocked cancel"):
        protocol.request("callback-error", {}, deadline=deadline, cancelled=cancelled)

    assert time.monotonic() < deadline + 0.2
    assert protocol.pending_count == 0
    assert protocol.fatal is True
    writer.released.set()
    protocol.close()


def test_duplicate_cancel_finalization_emits_only_one_cancel_frame() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    pending_ready = threading.Event()
    release_callback = threading.Event()

    def cancelled() -> bool:
        pending_ready.set()
        assert release_callback.wait(1)
        return True

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            protocol.request,
            "duplicate-cancel",
            {},
            deadline=time.monotonic() + 1,
            cancelled=cancelled,
        )
        assert pending_ready.wait(1)
        key = protocol.pending_keys[0]
        pending = protocol._pending[key]
        protocol._cancel_pending(key, pending)
        protocol._cancel_pending(key, pending)
        release_callback.set()
        with pytest.raises(RequestCancelled):
            future.result(timeout=1)

    deadline = time.monotonic() + 1
    while len(writer.frames) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len([frame for frame in writer.frames if b"$/cancelRequest" in frame]) == 1
    assert protocol.pending_count == 0
    protocol.close()
