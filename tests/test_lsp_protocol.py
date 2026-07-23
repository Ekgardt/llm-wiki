"""Strict, bounded, hostile-peer tests for the LSP transport."""

from __future__ import annotations

import io
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import lsp_protocol
import pytest
from lsp_protocol import (
    CANCEL_DRAIN_GRACE_SECONDS,
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
    CancellationSource,
    CancellationToken,
    JsonRpcFrameReader,
    JsonRpcResponseError,
    LspProtocol,
    PendingRequest,
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
    assert CANCEL_DRAIN_GRACE_SECONDS == 2.0
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


def test_cancellation_source_has_stable_read_only_sticky_token() -> None:
    source = CancellationSource()
    token = source.token

    assert source.token is token
    assert isinstance(token, CancellationToken)
    assert token.cancelled_at is None
    assert token.is_cancelled() is False
    assert token.wait(0) is False
    assert not hasattr(token, "cancel")
    assert not hasattr(token, "clear")
    with pytest.raises(AttributeError):
        token.cancelled_at = 1.0  # type: ignore[misc]

    assert source.cancel() is True
    cancelled_at = token.cancelled_at
    assert cancelled_at is not None
    assert token.is_cancelled() is True
    assert token.wait(0) is True
    assert source.cancel() is False
    assert token.cancelled_at == cancelled_at


def test_cancellation_source_is_thread_safe_and_records_one_timestamp() -> None:
    source = CancellationSource()
    barrier = threading.Barrier(16)

    def cancel() -> bool:
        barrier.wait()
        return source.cancel()

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _index: cancel(), range(16)))

    assert results.count(True) == 1
    assert results.count(False) == 15
    assert source.token.cancelled_at is not None


def test_pre_cancelled_and_expired_requests_allocate_nothing() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    source = CancellationSource()
    source.cancel()

    with pytest.raises(RequestCancelled):
        protocol.request(
            "pre-cancelled",
            {},
            deadline=time.monotonic() + 1,
            cancellation=source.token,
        )
    with pytest.raises(TimeoutError):
        protocol.request("expired", {}, deadline=time.monotonic() - 1)

    assert protocol.pending_count == 0
    assert protocol.pending_keys == ()
    assert writer.frames == []
    assert protocol._next_request_id == 1
    protocol.close()


def test_request_rejects_callable_cancellation_without_executing_it() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    called = False

    def arbitrary_code() -> bool:
        nonlocal called
        called = True
        return True

    with pytest.raises(TypeError, match="CancellationToken"):
        protocol.request(
            "unsafe",
            {},
            deadline=time.monotonic() + 1,
            cancellation=arbitrary_code,  # type: ignore[arg-type]
        )
    assert called is False
    assert writer.frames == []
    protocol.close()


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
    ],
)
def test_protocol_violation_is_fatal(scenario: str, fake_server: FakeLspServer) -> None:
    connection = fake_server.connection(scenario)
    with pytest.raises(ProtocolViolation):
        _request(connection)
    assert connection.fatal is True


def test_duplicate_response_is_fatal_without_overriding_first_response(
    fake_server: FakeLspServer,
) -> None:
    connection = fake_server.connection("duplicate-response-id")
    assert _request(connection) is None
    deadline = time.monotonic() + 1
    while not connection.fatal and time.monotonic() < deadline:
        time.sleep(0.001)
    assert connection.fatal is True
    with pytest.raises(ProtocolViolation, match="duplicate active response ID"):
        _request(connection)


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
    source = CancellationSource()
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
    timer = threading.Timer(0.03, source.cancel)
    timer.start()
    with pytest.raises(RequestCancelled):
        protocol.request("slow", {}, deadline=time.monotonic() + 1, cancellation=source.token)
    assert cancel_seen.wait(1)
    assert _request(protocol) == "fresh"
    assert protocol.fatal is False
    timer.join()


def test_timeout_keeps_sent_drain_without_waiting_past_original_deadline(
    fake_server: FakeLspServer,
) -> None:
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

    protocol = fake_server.start(handler)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        protocol.request("slow", {}, deadline=started + 0.05)
    assert time.monotonic() - started < 0.25
    assert protocol.pending_count == 1
    assert cancel_seen.wait(1)


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


def test_close_committed_before_cancellation_remains_caller_outcome() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    source = CancellationSource()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            protocol.request,
            "close-then-cancel",
            {},
            deadline=time.monotonic() + 1,
            cancellation=source.token,
        )
        while not writer.frames:
            time.sleep(0.001)
        protocol.close()
        source.cancel()
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
        "textDocument/prepareCallHierarchy",
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


@pytest.mark.parametrize("failure_owner", ["lsp-stdin-", "lsp-stdout-"])
def test_constructor_thread_start_failure_retains_no_owner_threads(
    monkeypatch: pytest.MonkeyPatch, failure_owner: str
) -> None:
    baseline = {thread.ident for thread in threading.enumerate() if thread.name.startswith("lsp-")}
    real_start = threading.Thread.start

    def fail_selected(thread: threading.Thread) -> None:
        if thread.name.startswith(failure_owner):
            raise RuntimeError("owner start failed")
        real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_selected)
    for index in range(25):
        reader = _BlockingReader()
        writer = _BlockingWriter(block_after=100)
        with pytest.raises(RuntimeError, match="owner start failed"):
            _protocol_with_streams(
                reader,
                writer,
                generation_nonce=f"constructor-start-failure-{index}",
            )
        assert reader.closed is True
        assert writer.closed is True
        assert {
            thread.ident
            for thread in threading.enumerate()
            if thread.name.startswith("lsp-")
        } == baseline


def test_constructor_startup_wait_failure_retains_no_owner_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = {thread.ident for thread in threading.enumerate() if thread.name.startswith("lsp-")}

    def fail_wait(_event: threading.Event, _owner_name: str) -> None:
        raise RuntimeError("startup wait failed")

    monkeypatch.setattr(LspProtocol, "_wait_owner_started", staticmethod(fail_wait))
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    with pytest.raises(RuntimeError, match="startup wait failed"):
        _protocol_with_streams(reader, writer, generation_nonce="constructor-wait-failure")
    assert reader.closed is True
    assert writer.closed is True
    assert {
        thread.ident for thread in threading.enumerate() if thread.name.startswith("lsp-")
    } == baseline


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


def _cancellation_threads() -> tuple[threading.Thread, ...]:
    return tuple(
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("lsp-cancel-")
    )


def test_queued_cancellation_writes_neither_request_nor_cancel() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter()
    protocol = _protocol_with_streams(reader, writer)
    protocol._write_message({"jsonrpc": "2.0", "method": "test/block"})
    assert writer.started.wait(1)
    source = CancellationSource()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            protocol.request,
            "queued",
            {},
            deadline=time.monotonic() + 1,
            cancellation=source.token,
        )
        deadline = time.monotonic() + 1
        while protocol.pending_count == 0 and time.monotonic() < deadline:
            time.sleep(0.001)
        assert source.cancel() is True
        with pytest.raises(RequestCancelled):
            future.result(timeout=0.2)

    writer.released.set()
    time.sleep(0.02)
    assert len(writer.frames) == 1
    assert b"test/block" in writer.frames[0]
    assert protocol.pending_count == 0
    protocol.close()


def test_writer_observes_queued_cancellation_before_requester() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter()
    protocol = _protocol_with_streams(reader, writer)
    protocol._write_message({"jsonrpc": "2.0", "method": "test/block"})
    assert writer.started.wait(1)
    source = CancellationSource()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            protocol.request,
            "writer-sees-cancel",
            {},
            deadline=time.monotonic() + 1,
            cancellation=source.token,
        )
        while protocol.pending_count == 0:
            time.sleep(0.001)
        pending = protocol._pending[protocol.pending_keys[0]]
        source.cancel()
        writer.released.set()
        with pytest.raises(RequestCancelled):
            future.result(timeout=1)

    time.sleep(0.02)
    assert len(writer.frames) == 1
    assert pending.terminal_source == "writer"
    assert protocol.fatal is False
    protocol.close()


def test_writer_observes_queued_expiry_without_writing_or_becoming_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter()
    protocol = _protocol_with_streams(reader, writer)
    protocol._write_message({"jsonrpc": "2.0", "method": "test/block"})
    assert writer.started.wait(1)
    deadline = time.monotonic() + 0.04

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(protocol.request, "writer-sees-expiry", {}, deadline=deadline)
        while protocol.pending_count == 0:
            time.sleep(0.001)
        pending = protocol._pending[protocol.pending_keys[0]]
        monkeypatch.setattr(
            lsp_protocol.time,
            "monotonic",
            lambda: deadline
            if threading.current_thread() is protocol.writer_thread
            else deadline - 0.01,
        )
        writer.released.set()
        with pytest.raises(TimeoutError):
            future.result(timeout=1)

    time.sleep(0.02)
    assert len(writer.frames) == 1
    assert pending.terminal_source == "writer"
    assert protocol.fatal is False
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
    source = CancellationSource()
    timer = threading.Timer(0.02, source.cancel)
    deadline = time.monotonic() + 0.08
    timer.start()

    with pytest.raises(RequestCancelled):
        protocol.request("cancel", {}, deadline=deadline, cancellation=source.token)

    assert time.monotonic() < deadline + 0.2
    assert writer.started.wait(1)
    assert protocol.pending_count == 1
    writer.released.set()
    delivery_deadline = time.monotonic() + 1
    while len(writer.frames) < 2 and time.monotonic() < delivery_deadline:
        time.sleep(0.005)
    assert len(writer.frames) == 2
    assert protocol.fatal is False
    timer.join()
    protocol.close()


def test_sending_cancellation_preserves_original_before_exactly_one_cancel() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter()
    protocol = _protocol_with_streams(reader, writer)
    source = CancellationSource()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            protocol.request,
            "sending-cancel",
            {},
            deadline=time.monotonic() + 1,
            cancellation=source.token,
        )
        assert writer.started.wait(1)
        key = protocol.pending_keys[0]
        assert protocol._pending[key].write_phase == "sending"
        source.cancel()
        with pytest.raises(RequestCancelled):
            future.result(timeout=0.2)
        assert writer.frames == []
        writer.released.set()

    deadline = time.monotonic() + 1
    while len(writer.frames) < 2 and time.monotonic() < deadline:
        time.sleep(0.001)
    assert len(writer.frames) == 2
    assert b'"method":"sending-cancel"' in writer.frames[0]
    assert b'"method":"$/cancelRequest"' in writer.frames[1]
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


def test_duplicate_cancel_finalization_emits_only_one_cancel_frame() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    source = CancellationSource()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            protocol.request,
            "duplicate-cancel",
            {},
            deadline=time.monotonic() + 1,
            cancellation=source.token,
        )
        deadline = time.monotonic() + 1
        while not writer.frames and time.monotonic() < deadline:
            time.sleep(0.001)
        key = protocol.pending_keys[0]
        pending = protocol._pending[key]
        assert source.cancel() is True
        assert source.cancel() is False
        pending.completed.set()
        pending.completed.set()
        with pytest.raises(RequestCancelled):
            future.result(timeout=1)

    deadline = time.monotonic() + 1
    while len(writer.frames) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len([frame for frame in writer.frames if b"$/cancelRequest" in frame]) == 1
    assert protocol.pending_count == 1
    protocol.close()


@pytest.mark.parametrize("method", ["callHierarchy/incomingCalls", "callHierarchy/outgoingCalls"])
def test_call_hierarchy_result_ceiling_accepts_10000_and_rejects_10001(
    method: str,
    fake_server: FakeLspServer,
) -> None:
    def handler(peer: FakeLspPeer) -> None:
        for count in (MAX_LOCATIONS, MAX_LOCATIONS + 1):
            request = peer.read()
            peer.send(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": [{"from": {}}] * count,
                }
            )

    protocol = fake_server.start(handler)
    result = protocol.request(method, {}, deadline=time.monotonic() + 2)
    assert len(result) == MAX_LOCATIONS
    with pytest.raises(ProtocolViolation, match="location"):
        protocol.request(method, {}, deadline=time.monotonic() + 2)


def _nested_symbols(count: int) -> list[dict[str, object]]:
    roots: list[dict[str, object]] = []
    remaining = count
    while remaining:
        children = min(99, remaining - 1)
        roots.append({"name": "root", "children": [{"name": "child"}] * children})
        remaining -= children + 1
    return roots


def test_nested_document_symbol_ceiling_counts_all_children(
    fake_server: FakeLspServer,
) -> None:
    def handler(peer: FakeLspPeer) -> None:
        for count in (MAX_LOCATIONS, MAX_LOCATIONS + 1):
            request = peer.read()
            peer.send(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": _nested_symbols(count),
                }
            )

    protocol = fake_server.start(handler)
    accepted = protocol.request(
        "textDocument/documentSymbol", {}, deadline=time.monotonic() + 2
    )
    assert sum(1 + len(root["children"]) for root in accepted) == MAX_LOCATIONS
    with pytest.raises(ProtocolViolation, match="location"):
        protocol.request("textDocument/documentSymbol", {}, deadline=time.monotonic() + 2)


def test_unrelated_result_is_not_subject_to_location_ceiling(
    fake_server: FakeLspServer,
) -> None:
    def handler(peer: FakeLspPeer) -> None:
        request = peer.read()
        peer.send(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": [None] * (MAX_LOCATIONS + 1),
            }
        )

    protocol = fake_server.start(handler)
    result = protocol.request("custom/arbitrary", {}, deadline=time.monotonic() + 2)
    assert len(result) == MAX_LOCATIONS + 1
    assert protocol.fatal is False


def test_response_dispatched_before_cancellation_wins() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    source = CancellationSource()

    def respond_then_cancel() -> None:
        while not writer.frames:
            time.sleep(0.001)
        protocol._dispatch_message(
            {"jsonrpc": "2.0", "id": 1, "result": "response-won"},
            generation_nonce="stream-probe",
        )
        time.sleep(0.002)
        source.cancel()

    thread = threading.Thread(target=respond_then_cancel)
    thread.start()
    assert protocol.request(
        "response-race",
        {},
        deadline=time.monotonic() + 1,
        cancellation=source.token,
    ) == "response-won"
    thread.join()
    assert protocol.pending_count == 0
    assert len(writer.frames) == 1
    protocol.close()


def test_response_committed_before_fatal_remains_caller_outcome() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_request, protocol, "response-then-fatal", 1)
        while not writer.frames:
            time.sleep(0.001)
        pending = protocol._pending[protocol.pending_keys[0]]
        original_wakeup = pending.completed
        pending.completed = threading.Event()
        protocol._dispatch_message(
            {"jsonrpc": "2.0", "id": 1, "result": "response-won"},
            generation_nonce="stream-probe",
        )
        protocol._become_fatal("after response")
        original_wakeup.set()
        assert future.result(timeout=1) == "response-won"
    protocol.close()


def test_fatal_committed_before_cancellation_remains_caller_outcome() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    source = CancellationSource()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            protocol.request,
            "fatal-then-cancel",
            {},
            deadline=time.monotonic() + 1,
            cancellation=source.token,
        )
        while not writer.frames:
            time.sleep(0.001)
        protocol._become_fatal("fatal won")
        source.cancel()
        with pytest.raises(ProtocolViolation, match="fatal won"):
            future.result(timeout=1)
    protocol.close()


def test_cancellation_timestamp_before_fatal_remains_caller_outcome() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    source = CancellationSource()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            protocol.request,
            "cancel-then-fatal",
            {},
            deadline=time.monotonic() + 1,
            cancellation=source.token,
        )
        while not writer.frames:
            time.sleep(0.001)
        source.cancel()
        protocol._become_fatal("fatal was later")
        with pytest.raises(RequestCancelled):
            future.result(timeout=1)
    protocol.close()


def test_cancellation_timestamp_before_response_wins_processing_race() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    source = CancellationSource()

    def cancel_then_respond() -> None:
        while not writer.frames:
            time.sleep(0.001)
        source.cancel()
        protocol._dispatch_message(
            {"jsonrpc": "2.0", "id": 1, "result": "too-late"},
            generation_nonce="stream-probe",
        )

    thread = threading.Thread(target=cancel_then_respond)
    thread.start()
    with pytest.raises(RequestCancelled):
        protocol.request(
            "cancel-race",
            {},
            deadline=time.monotonic() + 1,
            cancellation=source.token,
        )
    thread.join()
    assert protocol.pending_count == 0
    deadline = time.monotonic() + 1
    while len(writer.frames) < 2 and time.monotonic() < deadline:
        time.sleep(0.001)
    assert len([frame for frame in writer.frames if b"$/cancelRequest" in frame]) == 1
    protocol.close()


def test_equal_cancellation_and_response_timestamps_prefer_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    source = CancellationSource()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            protocol.request,
            "timestamp-tie",
            {},
            deadline=time.monotonic() + 1,
            cancellation=source.token,
        )
        while not writer.frames:
            time.sleep(0.001)
        tied_at = time.monotonic()
        monkeypatch.setattr(lsp_protocol.time, "monotonic", lambda: tied_at)
        source.cancel()
        protocol._dispatch_message(
            {"jsonrpc": "2.0", "id": 1, "result": "tie"},
            generation_nonce="stream-probe",
        )
        with pytest.raises(RequestCancelled):
            future.result(timeout=1)
    protocol.close()


def test_equal_cancellation_and_deadline_timestamps_prefer_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    source = CancellationSource()
    deadline = time.monotonic() + 1

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            protocol.request,
            "cancel-deadline-tie",
            {},
            deadline=deadline,
            cancellation=source.token,
        )
        while not writer.frames:
            time.sleep(0.001)
        monkeypatch.setattr(lsp_protocol.time, "monotonic", lambda: deadline)
        source.cancel()
        with pytest.raises(RequestCancelled):
            future.result(timeout=1)
    protocol.close()


def test_response_dispatched_at_or_after_deadline_cannot_win() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    deadline = time.monotonic() + 0.03

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(protocol.request, "late", {}, deadline=deadline)
        while not writer.frames:
            time.sleep(0.001)
        while time.monotonic() < deadline:
            time.sleep(0.001)
        protocol._dispatch_message(
            {"jsonrpc": "2.0", "id": 1, "result": "too-late"},
            generation_nonce="stream-probe",
        )
        with pytest.raises(TimeoutError):
            future.result(timeout=1)
    assert protocol.pending_count == 0
    protocol.close()


def test_cancellation_creates_no_threads_across_generations() -> None:
    before = _cancellation_threads()
    for index in range(12):
        protocol = _protocol_with_streams(
            _BlockingReader(),
            _BlockingWriter(block_after=100),
            generation_nonce=f"token-generation-{index}",
        )
        source = CancellationSource()
        source.cancel()
        with pytest.raises(RequestCancelled):
            protocol.request(
                "pre-cancelled",
                {},
                deadline=time.monotonic() + 1,
                cancellation=source.token,
            )
        protocol.close()
    assert _cancellation_threads() == before == ()


def test_sent_cancelled_requests_remain_charged_until_late_responses() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    sources = [CancellationSource() for _ in range(MAX_PENDING_REQUESTS)]

    with ThreadPoolExecutor(max_workers=MAX_PENDING_REQUESTS) as pool:
        futures = [
            pool.submit(
                protocol.request,
                "drain",
                {},
                deadline=time.monotonic() + 3,
                cancellation=source.token,
            )
            for source in sources
        ]
        deadline = time.monotonic() + 1
        while len(writer.frames) < MAX_PENDING_REQUESTS and time.monotonic() < deadline:
            time.sleep(0.001)
        assert len(writer.frames) == MAX_PENDING_REQUESTS
        for source in sources:
            source.cancel()
        for future in futures:
            with pytest.raises(RequestCancelled):
                future.result(timeout=1)

    assert protocol.pending_count == MAX_PENDING_REQUESTS
    assert protocol._next_request_id == MAX_PENDING_REQUESTS + 1
    with pytest.raises(PendingRequestLimitExceeded):
        _request(protocol)

    for request_id in range(1, MAX_PENDING_REQUESTS + 1):
        protocol._dispatch_message(
            {"jsonrpc": "2.0", "id": request_id, "result": "late"},
            generation_nonce="stream-probe",
        )
    assert protocol.pending_count == 0
    assert protocol.pending_keys == ()
    protocol.close()


def test_ordinary_writes_cannot_consume_reserved_control_capacity() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter()
    protocol = _protocol_with_streams(reader, writer)
    protocol._write_message({"jsonrpc": "2.0", "method": "test/block"})
    assert writer.started.wait(1)
    ordinary_limit = lsp_protocol._MAX_QUEUED_WRITES - MAX_PENDING_REQUESTS

    for index in range(ordinary_limit):
        protocol._write_message(
            {"jsonrpc": "2.0", "method": "test/ordinary", "params": {"index": index}}
        )
    with pytest.raises(TimeoutError, match="queue"):
        protocol._write_message({"jsonrpc": "2.0", "method": "test/overflow"})
    assert protocol.fatal is True
    protocol.close()


def test_full_ordinary_queue_still_accepts_all_active_cancellations() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=MAX_PENDING_REQUESTS)
    protocol = _protocol_with_streams(reader, writer)
    sources = [CancellationSource() for _ in range(MAX_PENDING_REQUESTS)]

    with ThreadPoolExecutor(max_workers=MAX_PENDING_REQUESTS) as pool:
        futures = [
            pool.submit(
                protocol.request,
                "reserved-cancel",
                {},
                deadline=time.monotonic() + 3,
                cancellation=source.token,
            )
            for source in sources
        ]
        deadline = time.monotonic() + 1
        while len(writer.frames) < MAX_PENDING_REQUESTS and time.monotonic() < deadline:
            time.sleep(0.001)
        assert len(writer.frames) == MAX_PENDING_REQUESTS
        protocol._write_message({"jsonrpc": "2.0", "method": "test/block"})
        assert writer.started.wait(1)
        ordinary_limit = lsp_protocol._MAX_QUEUED_WRITES - MAX_PENDING_REQUESTS
        for index in range(ordinary_limit):
            protocol._write_message(
                {
                    "jsonrpc": "2.0",
                    "method": "test/ordinary",
                    "params": {"index": index},
                },
                deadline=time.monotonic() + 3,
            )
        assert protocol._ordinary_queued == ordinary_limit
        for source in sources:
            source.cancel()
        for future in futures:
            with pytest.raises(RequestCancelled):
                future.result(timeout=0.5)
        assert protocol._control_queued == MAX_PENDING_REQUESTS
        assert protocol._write_queue.qsize() == lsp_protocol._MAX_QUEUED_WRITES
        writer.released.set()

    deadline = time.monotonic() + 2
    while len([frame for frame in writer.frames if b"$/cancelRequest" in frame]) < 32:
        assert time.monotonic() < deadline
        time.sleep(0.001)
    assert len([frame for frame in writer.frames if b"$/cancelRequest" in frame]) == 32
    assert protocol.fatal is False
    protocol.close()


def test_request_ids_are_not_reused_after_drain_release() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    source = CancellationSource()

    with ThreadPoolExecutor(max_workers=1) as pool:
        cancelled = pool.submit(
            protocol.request,
            "first",
            {},
            deadline=time.monotonic() + 2,
            cancellation=source.token,
        )
        while not writer.frames:
            time.sleep(0.001)
        source.cancel()
        with pytest.raises(RequestCancelled):
            cancelled.result(timeout=1)
        protocol._dispatch_message(
            {"jsonrpc": "2.0", "id": 1, "result": "late"},
            generation_nonce="stream-probe",
        )
        fresh = pool.submit(_request, protocol, "second", 1)
        while protocol.pending_keys != (("stream-probe", 2),):
            time.sleep(0.001)
        protocol._dispatch_message(
            {"jsonrpc": "2.0", "id": 2, "result": "fresh"},
            generation_nonce="stream-probe",
        )
        assert fresh.result(timeout=1) == "fresh"
    assert protocol._next_request_id == 3
    protocol.close()


def test_late_peer_cancellation_drains_without_changing_local_outcome() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    source = CancellationSource()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            protocol.request,
            "late-peer-cancel",
            {},
            deadline=time.monotonic() + 1,
            cancellation=source.token,
        )
        deadline = time.monotonic() + 1
        while not writer.frames and time.monotonic() < deadline:
            time.sleep(0.001)
        source.cancel()
        with pytest.raises(RequestCancelled):
            future.result(timeout=1)

    protocol._dispatch_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32800, "message": "cancelled"},
        },
        generation_nonce="stream-probe",
    )
    assert protocol.pending_count == 0
    protocol.close()


def test_close_releases_drain_only_requests() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    source = CancellationSource()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            protocol.request,
            "close-drain",
            {},
            deadline=time.monotonic() + 1,
            cancellation=source.token,
        )
        while not writer.frames:
            time.sleep(0.001)
        source.cancel()
        with pytest.raises(RequestCancelled):
            future.result(timeout=1)
    assert protocol.pending_count == 1
    pending = protocol._pending[protocol.pending_keys[0]]
    cancelled_at = source.token.cancelled_at
    assert cancelled_at is not None
    assert pending.drain_deadline == cancelled_at + CANCEL_DRAIN_GRACE_SECONDS
    protocol.close()
    assert protocol.pending_count == 0


def test_expired_drain_keys_are_validated_read_only_and_sorted() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol = _protocol_with_streams(reader, writer)
    now = time.monotonic()
    with protocol._state_lock:
        for request_id, drain_deadline in ((2, now - 1), (1, now), (3, now + 1)):
            protocol._pending[("stream-probe", request_id)] = PendingRequest(
                request_id,
                "drain",
                "stream-probe",
                now + 5,
                threading.Event(),
                drain_deadline=drain_deadline,
            )

    assert protocol.expired_drain_keys(now) == (
        ("stream-probe", 1),
        ("stream-probe", 2),
    )
    assert protocol.pending_count == 3
    with pytest.raises(TypeError):
        protocol.expired_drain_keys(True)
    with pytest.raises(ValueError):
        protocol.expired_drain_keys(float("inf"))
    protocol.close()
