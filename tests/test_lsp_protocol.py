"""Strict, bounded, hostile-peer tests for the LSP transport."""

from __future__ import annotations

import io
import json
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


def test_timeout_sends_cancel_without_waiting_past_original_deadline(
    fake_server: FakeLspServer,
) -> None:
    cancel_seen = threading.Event()

    def handler(peer: FakeLspPeer) -> None:
        request = peer.read()
        cancellation = peer.read()
        assert cancellation["params"] == {"id": request["id"]}
        cancel_seen.set()

    protocol = fake_server.start(handler)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        protocol.request("slow", {}, deadline=started + 0.05)
    assert time.monotonic() - started < 0.25
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
