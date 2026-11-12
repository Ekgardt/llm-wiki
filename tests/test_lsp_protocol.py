"""Strict, bounded, hostile-peer tests for the LSP transport."""

from __future__ import annotations

import json
import os
import socket
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
    JsonRpcResponseError,
    LspProtocol,
    PendingRequest,
    PendingRequestLimitExceeded,
    ProtocolViolation,
    RequestCancelled,
    ResponseRefused,
)

from tests.fake_lsp_server import FakeLspPeer, FakeLspServer
from tests.lsp_protocol_helpers import (
    _BlockingReader,
    _BlockingWriter,
    _frames_before_drain_marker,
    _hold_the_writer_on_a_frame,
    _join_io_threads,
    _protocol_on_blocking_streams,
    _protocol_with_streams,
)
from tests.slow_machine import LONG_TIMEOUT, SHORT_TIMEOUT


@pytest.fixture(autouse=True, params=[False, True], ids=["posix-close", "windows-close"])
def _close_path(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every case runs through both close paths, not only the one of the host.

    The Windows path (an owner closes its own stream) never ran on Linux, so a
    `fileno()` on a stream its owner had already closed raised ValueError out of
    every close on Windows CI only (run 36327902173). A case that needs one path
    sets the flag itself; its own monkeypatch runs after this one.
    """
    monkeypatch.setattr(lsp_protocol, "CLOSE_WAITS_FOR_A_READ", request.param)


@pytest.fixture
def fake_server() -> FakeLspServer:
    server = FakeLspServer()
    yield server
    server.close()


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
    # Since 2026-09-17 (finding K-B16) this is derived from the profile
    # registry, so a profile added there is carried by the transport.
    assert SERVER_NOTIFICATIONS == frozenset(
        {
            "$/progress",
            "$/typescriptVersion",
            "experimental/serverStatus",
            "pyright/beginProgress",
            "pyright/endProgress",
            "pyright/reportProgress",
            "textDocument/publishDiagnostics",
            "window/logMessage",
            "window/showMessage",
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
    assert token.wait(SHORT_TIMEOUT) is True
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
    reader, writer, protocol = _protocol_on_blocking_streams()
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
    reader, writer, protocol = _protocol_on_blocking_streams()
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
    assert peer_ready.wait(SHORT_TIMEOUT)
    _join_io_threads(protocol)
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
        assert release.wait(SHORT_TIMEOUT)
        for request in reversed(received):
            peer.send({"jsonrpc": "2.0", "id": request["id"], "result": request["id"]})

    protocol = fake_server.start(handler)
    with ThreadPoolExecutor(max_workers=MAX_PENDING_REQUESTS) as pool:
        futures = [pool.submit(_request, protocol, "example", 3) for _ in range(32)]
        assert all_received.wait(SHORT_TIMEOUT)
        with pytest.raises(PendingRequestLimitExceeded):
            _request(protocol, timeout=1)
        release.set()
        assert sorted(future.result(timeout=SHORT_TIMEOUT) for future in futures) == list(range(1, 33))
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
        assert [future.result(timeout=SHORT_TIMEOUT) for future in futures] == [None] * 32
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
    assert cancel_seen.wait(SHORT_TIMEOUT)
    assert _request(protocol) == "fresh"
    assert protocol.fatal is False
    timer.join()


def test_sent_request_evidence_only_advances_after_full_dispatch() -> None:
    reader, writer, protocol = _protocol_on_blocking_streams(0)
    source = CancellationSource()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            protocol.request,
            "slow",
            {},
            deadline=time.monotonic() + SHORT_TIMEOUT,
            cancellation=source.token,
        )
        deadline = time.monotonic() + 1
        while protocol.pending_count == 0 and time.monotonic() < deadline:
            time.sleep(0.001)
        assert protocol.pending_count == 1
        assert protocol._sent_request_evidence() == (0, None)

        writer.released.set()
        while protocol._sent_request_evidence()[0] == 0 and time.monotonic() < deadline:
            time.sleep(0.001)
        assert protocol._sent_request_evidence() == (1, "slow")

        source.cancel()
        with pytest.raises(RequestCancelled):
            future.result(timeout=1)

    protocol.close()


def test_immediate_responses_preserve_monotonic_dispatch_evidence(
    fake_server: FakeLspServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_count = 100
    real_write_frame = LspProtocol._write_frame
    real_complete_write = LspProtocol._complete_write
    completed_writes = 0
    completed_condition = threading.Condition()

    def force_response_before_post_write_state(
        protocol: LspProtocol,
        task: object,
    ) -> None:
        real_write_frame(protocol, task)
        if task.request_key is None:
            return
        deadline = time.monotonic() + 1
        while protocol.pending_count and time.monotonic() < deadline:
            time.sleep(0.001)
        assert protocol.pending_count == 0

    def track_completed_write(
        protocol: LspProtocol,
        task: object,
        error: BaseException | None,
    ) -> None:
        nonlocal completed_writes
        real_complete_write(protocol, task, error)
        if task.request_key is not None and error is None:
            with completed_condition:
                completed_writes += 1
                completed_condition.notify_all()

    monkeypatch.setattr(LspProtocol, "_write_frame", force_response_before_post_write_state)
    monkeypatch.setattr(LspProtocol, "_complete_write", track_completed_write)

    def handler(peer: FakeLspPeer) -> None:
        for _index in range(request_count):
            request = peer.read()
            peer.send({"jsonrpc": "2.0", "id": request["id"], "result": None})

    protocol = fake_server.start(handler)
    for index in range(1, request_count + 1):
        assert protocol.request(
            "immediate",
            {},
            deadline=time.monotonic() + SHORT_TIMEOUT,
        ) is None
        with completed_condition:
            assert completed_condition.wait_for(
                lambda: completed_writes >= index,
                timeout=1,
            )
        assert protocol.pending_count == 0
    assert completed_writes == request_count
    assert protocol._sent_request_evidence() == (request_count, "immediate")


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
    assert cancel_seen.wait(SHORT_TIMEOUT)


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
        assert request_seen.wait(SHORT_TIMEOUT)
        protocol.close()
        with pytest.raises(ProtocolViolation, match="closed"):
            future.result(timeout=1)


def test_notification_uses_owned_writer_and_waits_for_delivery() -> None:
    reader, writer, protocol = _protocol_on_blocking_streams()
    deadline = time.monotonic() + 1

    protocol.notify("exit", {}, deadline=deadline)

    assert len(writer.frames) == 1
    assert b'"method":"exit"' in writer.frames[0]
    assert protocol.stdin_writer_owner == protocol.writer_thread.ident
    protocol.close()


def _cancel_all_and_expect(sources, futures, *, timeout: float) -> None:
    """Cancel every source; every caller must observe RequestCancelled."""
    for source in sources:
        source.cancel()
    for future in futures:
        with pytest.raises(RequestCancelled):
            future.result(timeout=timeout)


def _dispatch_late_responses(protocol, count: int) -> None:
    for request_id in range(1, count + 1):
        protocol._dispatch_message(
            {"jsonrpc": "2.0", "id": request_id, "result": "late"},
            generation_nonce="stream-probe",
        )


def _linked_errors(error: BaseException) -> list[BaseException]:
    return [item for item in (error.__cause__, error.__context__) if item is not None]


def _reachable_errors(error: BaseException) -> list[BaseException]:
    """Every error reachable through cause and context, each visited once."""
    pending = [error]
    seen: set[int] = set()
    reachable: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        reachable.append(current)
        pending.extend(_linked_errors(current))
    return reachable


def _first_retained_cleanup(errors):
    return next(
        (
            error
            for error in errors
            if isinstance(error, lsp_protocol.ProtocolStartupCleanupError)
        ),
        None,
    )


def _assert_error_graph_is_acyclic(error: BaseException) -> None:
    pending = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        assert id(current) not in visited
        visited.add(id(current))
        pending.extend(_linked_errors(current))


def _lsp_thread_idents() -> set[int]:
    return {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith("lsp-")
    }


def _await_cancel_frames(writer, count: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while _cancel_frames(writer) < count and time.monotonic() < deadline:
        time.sleep(0.001)


def _all_requests_sent(protocol, count: int) -> bool:
    with protocol._state_lock:
        if len(protocol._pending) != count:
            return False
        return all(
            pending.write_phase == "sent" for pending in protocol._pending.values()
        )


def _await_all_sent(protocol, count: int, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _all_requests_sent(protocol, count):
            return True
        time.sleep(0.001)
    return False


def _await_frames(writer, count: int, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while len(writer.frames) < count and time.monotonic() < deadline:
        time.sleep(0.001)


def _cancel_frames(writer) -> int:
    return len([frame for frame in writer.frames if b"$/cancelRequest" in frame])


def test_cancel_all_releases_pending_callers_and_sends_one_cancel_each() -> None:
    reader, writer, protocol = _protocol_on_blocking_streams()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                protocol.request,
                "pending",
                {},
                deadline=time.monotonic() + 2,
            )
            for _ in range(2)
        ]
        assert _await_all_sent(protocol, 2)
        protocol.cancel_all("manager shutdown")
        errors = [future.exception(timeout=1) for future in futures]
    assert all(isinstance(error, RequestCancelled) for error in errors)

    _await_frames(writer, 4)
    assert _cancel_frames(writer) == 2
    assert protocol.pending_count == 2
    protocol.close()


def test_close_committed_before_cancellation_remains_caller_outcome() -> None:
    reader, writer, protocol = _protocol_on_blocking_streams()
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
    assert response_sent.wait(SHORT_TIMEOUT)
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
    assert len(protocol.request(method, {}, deadline=time.monotonic() + SHORT_TIMEOUT)) == MAX_LOCATIONS
    with pytest.raises(ResponseRefused, match="location"):
        protocol.request(method, {}, deadline=time.monotonic() + SHORT_TIMEOUT)
    # One answer over the bound refuses its request, not the connection (audit C-38).
    assert protocol.fatal is False


def _wait_until_filled(items: list, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not items and time.monotonic() < deadline:
        time.sleep(0.005)


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

    warnings: list[str] = []
    protocol = fake_server.start(
        handler,
        warning_callback=warnings.append,
        server_notification_handlers={"textDocument/publishDiagnostics": received.append},
    )
    assert first_sent.wait(SHORT_TIMEOUT)
    _wait_until_filled(received, 1)
    assert len(received[0]["diagnostics"]) == MAX_DIAGNOSTICS
    with pytest.raises(TimeoutError):
        _request(protocol, timeout=0.3)
    _wait_until_filled(warnings, LONG_TIMEOUT)
    # The oversized notification is dropped with a warning; the connection stays.
    assert (len(received), protocol.fatal) == (1, False)
    assert any("diagnostic" in warning for warning in warnings)


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
    result = protocol.request("textDocument/hover", {}, deadline=time.monotonic() + SHORT_TIMEOUT)
    assert len(json.dumps(result, separators=(",", ":")).encode()) == MAX_HOVER_BYTES
    with pytest.raises(ResponseRefused, match="hover"):
        protocol.request("textDocument/hover", {}, deadline=time.monotonic() + SHORT_TIMEOUT)
    assert protocol.fatal is False


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
    assert completed.wait(SHORT_TIMEOUT)
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
    assert completed.wait(SHORT_TIMEOUT)
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
    assert completed.wait(SHORT_TIMEOUT)
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
    assert completed.wait(SHORT_TIMEOUT)
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
    baseline = _lsp_thread_idents()
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
    baseline = _lsp_thread_idents()

    def fail_wait(
        _event: threading.Event, _owner_name: str, _deadline: float
    ) -> None:
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


def test_constructor_owner_waits_and_rollback_share_startup_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wait_deadlines: list[float] = []
    rollback_deadlines: list[float] = []
    real_wait = LspProtocol._wait_owner_started
    real_join = LspProtocol._join_partially_started_owner

    def fail_after_reader_starts(
        event: threading.Event, owner_name: str, deadline: float
    ) -> None:
        wait_deadlines.append(deadline)
        real_wait(event, owner_name, deadline)
        if owner_name == "reader":
            raise RuntimeError("injected startup failure")

    def record_rollback(
        self: LspProtocol, owner: threading.Thread, deadline: float
    ) -> None:
        rollback_deadlines.append(deadline)
        real_join(self, owner, deadline)

    monkeypatch.setattr(
        LspProtocol, "_wait_owner_started", staticmethod(fail_after_reader_starts)
    )
    monkeypatch.setattr(LspProtocol, "_join_partially_started_owner", record_rollback)
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    startup_deadline = time.monotonic() + 1

    with pytest.raises(RuntimeError, match="injected startup failure"):
        LspProtocol(
            reader,
            writer,
            "inherited-startup-deadline",
            fatal_callback=lambda _reason: None,
            _startup_deadline=startup_deadline,
        )

    assert wait_deadlines == [startup_deadline, startup_deadline]
    assert rollback_deadlines == [startup_deadline, startup_deadline]


def test_constructor_accepts_registration_recorded_before_delayed_event_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_register = LspProtocol._register_owner
    registrations = {
        "reader": threading.Event(),
        "writer": threading.Event(),
    }

    def register_before_publication(
        protocol: LspProtocol,
        name: str,
        started: threading.Event,
    ) -> bool:
        real_set = started.set
        started.set = lambda: None
        try:
            registered = real_register(protocol, name, started)
        finally:
            started.set = real_set
        registrations[name].set()
        return registered

    def observe_delayed_publication(
        event: threading.Event,
        owner_name: str,
        _deadline: float,
    ) -> None:
        assert registrations[owner_name].wait(SHORT_TIMEOUT)
        assert event.is_set() is False
        raise TimeoutError(f"LSP {owner_name} owner did not start before deadline")

    monkeypatch.setattr(LspProtocol, "_register_owner", register_before_publication)
    monkeypatch.setattr(
        LspProtocol,
        "_wait_owner_started",
        staticmethod(observe_delayed_publication),
    )
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)

    protocol = LspProtocol(
        reader,
        writer,
        "owner-registration-before-publication",
        fatal_callback=lambda _reason: None,
        _startup_deadline=time.monotonic() + 1,
    )
    try:
        assert protocol.stdout_reader_owner == protocol.reader_thread.ident
        assert protocol.stdin_writer_owner == protocol.writer_thread.ident
    finally:
        protocol.close(time.monotonic() + 1)


def test_constructor_rollback_rethrows_real_join_interruption_with_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_join = threading.Thread.join
    real_register = LspProtocol._register_owner
    interrupted = False
    raised_error: BaseException | None = None

    def fail_reader_registration(
        protocol: LspProtocol,
        name: str,
        started: threading.Event,
    ) -> bool:
        if name != "reader":
            return real_register(protocol, name, started)
        with protocol._owner_handle_lock:
            protocol._owner_start_errors[name] = RuntimeError(
                "reader registration failed"
            )
        started.set()
        return False

    def interrupt_writer_join(
        thread: threading.Thread,
        timeout: float | None = None,
    ) -> None:
        nonlocal interrupted
        if thread.name.startswith("lsp-stdin-") and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("owner join interrupted")
        real_join(thread, timeout)

    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    with monkeypatch.context() as patch:
        patch.setattr(LspProtocol, "_register_owner", fail_reader_registration)
        patch.setattr(threading.Thread, "join", interrupt_writer_join)
        try:
            LspProtocol(
                reader,
                writer,
                "real-join-interruption",
                fatal_callback=lambda _reason: None,
                _startup_deadline=time.monotonic() + 1,
            )
        except BaseException as error:
            raised_error = error

    assert raised_error is not None
    retained = (
        raised_error
        if isinstance(raised_error, lsp_protocol.ProtocolStartupCleanupError)
        else raised_error.__cause__
    )
    assert isinstance(retained, lsp_protocol.ProtocolStartupCleanupError)
    retained.protocol.close(time.monotonic() + 1)

    assert isinstance(raised_error, KeyboardInterrupt)
    assert str(raised_error) == "owner join interrupted"
    traceback_names: list[str] = []
    current = raised_error.__traceback__
    while current is not None:
        traceback_names.append(current.tb_frame.f_code.co_name)
        current = current.tb_next
    assert "interrupt_writer_join" in traceback_names
    assert raised_error.__cause__ is retained
    assert isinstance(retained.__cause__, RuntimeError)
    assert str(retained.__cause__) == "reader registration failed"


def test_constructor_startup_interruption_outranks_ordinary_rollback_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_start = threading.Thread.start
    real_join = threading.Thread.join
    raised_error: BaseException | None = None

    def interrupt_reader_start(thread: threading.Thread) -> None:
        if thread.name.startswith("lsp-stdout-"):
            raise KeyboardInterrupt("reader startup interrupted")
        real_start(thread)

    def fail_writer_join(
        thread: threading.Thread,
        timeout: float | None = None,
    ) -> None:
        if thread.name.startswith("lsp-stdin-"):
            raise TimeoutError("ordinary writer rollback timeout")
        real_join(thread, timeout)

    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", interrupt_reader_start)
        patch.setattr(threading.Thread, "join", fail_writer_join)
        try:
            LspProtocol(
                reader,
                writer,
                "startup-interruption-priority",
                fatal_callback=lambda _reason: None,
                _startup_deadline=time.monotonic() + 1,
            )
        except BaseException as error:
            raised_error = error

    assert raised_error is not None
    retained = (
        raised_error
        if isinstance(raised_error, lsp_protocol.ProtocolStartupCleanupError)
        else raised_error.__cause__
    )
    assert isinstance(retained, lsp_protocol.ProtocolStartupCleanupError)
    retained.protocol.close(time.monotonic() + 1)

    assert isinstance(raised_error, KeyboardInterrupt)
    assert str(raised_error) == "reader startup interrupted"
    traceback_names: list[str] = []
    current = raised_error.__traceback__
    while current is not None:
        traceback_names.append(current.tb_frame.f_code.co_name)
        current = current.tb_next
    assert "interrupt_reader_start" in traceback_names
    assert raised_error.__cause__ is retained
    assert any(isinstance(error, TimeoutError) for error in retained.errors)


def test_constructor_unwraps_startup_interruption_without_exception_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_start = threading.Thread.start

    def make_interruption() -> KeyboardInterrupt:
        try:
            raise KeyboardInterrupt("wrapped protocol startup interruption")
        except KeyboardInterrupt as error:
            return error

    interruption = make_interruption()
    wrapper = RuntimeError("protocol startup interruption wrapper")
    wrapper.__cause__ = interruption

    def wrapped_writer_start(thread: threading.Thread) -> None:
        if thread.name.startswith("lsp-stdin-"):
            raise wrapper
        real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", wrapped_writer_start)
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)

    with pytest.raises(
        KeyboardInterrupt,
        match="wrapped protocol startup interruption",
    ) as raised:
        LspProtocol(
            reader,
            writer,
            "wrapped-constructor-startup-interruption",
            fatal_callback=lambda _reason: None,
            _startup_deadline=time.monotonic() + 1,
        )

    assert raised.value is interruption
    assert raised.value.__cause__ is wrapper
    assert wrapper.__cause__ is None
    assert wrapper.__context__ is None
    assert reader.closed is True
    assert writer.closed is True


def test_constructor_partial_owner_retains_cleanup_behind_acyclic_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_start = threading.Thread.start
    real_partial_join = LspProtocol._join_partially_started_owner

    def make_interruption() -> KeyboardInterrupt:
        try:
            raise KeyboardInterrupt("partial-owner startup interruption")
        except KeyboardInterrupt as error:
            return error

    interruption = make_interruption()
    wrapper = RuntimeError("partial-owner startup wrapper")
    wrapper.__cause__ = interruption
    join_error = TimeoutError("partial writer owner retained")

    def start_writer_then_interrupt(thread: threading.Thread) -> None:
        real_start(thread)
        if thread.name.startswith("lsp-stdin-"):
            raise wrapper

    def retain_writer_owner(
        self: LspProtocol,
        owner: threading.Thread,
        deadline: float,
    ) -> None:
        if owner is self.writer_thread:
            raise join_error
        real_partial_join(self, owner, deadline)

    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    raised_error: BaseException | None = None
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", start_writer_then_interrupt)
        patch.setattr(
            LspProtocol,
            "_join_partially_started_owner",
            retain_writer_owner,
        )
        try:
            LspProtocol(
                reader,
                writer,
                "partial-owner-nested-interruption",
                fatal_callback=lambda _reason: None,
                _startup_deadline=time.monotonic() + 1,
            )
        except BaseException as error:
            raised_error = error

    assert raised_error is not None
    reachable = _reachable_errors(raised_error)
    retained = _first_retained_cleanup(reachable)
    assert retained is not None
    try:
        _assert_error_graph_is_acyclic(raised_error)

        assert raised_error is interruption
        assert retained in reachable
        assert wrapper in reachable
        assert retained.errors == (join_error,)
        assert wrapper.__cause__ is None
        assert wrapper.__context__ is None
    finally:
        retained.protocol.close(time.monotonic() + 1)

    assert not retained.protocol.reader_thread.is_alive()
    assert not retained.protocol.writer_thread.is_alive()


def test_join_owners_later_interruption_outranks_first_ordinary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = _protocol_with_streams(
        _BlockingReader(),
        _BlockingWriter(block_after=100),
        generation_nonce="join-owner-interruption-priority",
    )
    real_join = threading.Thread.join
    raised_error: BaseException | None = None

    def ordered_join_failures(
        thread: threading.Thread,
        timeout: float | None = None,
    ) -> None:
        if thread.name.startswith("lsp-stdout-"):
            raise TimeoutError("first ordinary owner join error")
        if thread.name.startswith("lsp-stdin-"):
            raise SystemExit(37)
        real_join(thread, timeout)

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "join", ordered_join_failures)
        try:
            protocol.close(time.monotonic() + 1)
        except BaseException as error:
            raised_error = error

    protocol.close(time.monotonic() + 1)
    assert isinstance(raised_error, SystemExit)
    assert raised_error.code == 37
    traceback_names: list[str] = []
    current = raised_error.__traceback__
    while current is not None:
        traceback_names.append(current.tb_frame.f_code.co_name)
        current = current.tb_next
    assert "ordered_join_failures" in traceback_names
    assert isinstance(raised_error.__cause__, TimeoutError)
    assert str(raised_error.__cause__) == "first ordinary owner join error"


def test_join_owners_unwraps_earliest_interruption_without_exception_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = _protocol_with_streams(
        _BlockingReader(),
        _BlockingWriter(block_after=100),
        generation_nonce="join-owner-wrapped-interruption",
    )
    real_join = threading.Thread.join
    ordinary_error = TimeoutError("later ordinary owner join error")

    def make_interruption() -> KeyboardInterrupt:
        try:
            raise KeyboardInterrupt("wrapped owner join interruption")
        except KeyboardInterrupt as error:
            return error

    interruption = make_interruption()
    wrapper = RuntimeError("owner join wrapper")
    wrapper.__cause__ = interruption
    raised_error: BaseException | None = None

    def wrapped_join_failures(
        thread: threading.Thread,
        timeout: float | None = None,
    ) -> None:
        if thread.name.startswith("lsp-stdout-"):
            raise wrapper
        if thread.name.startswith("lsp-stdin-"):
            raise ordinary_error
        real_join(thread, timeout)

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "join", wrapped_join_failures)
        try:
            protocol.close(time.monotonic() + 1)
        except BaseException as error:
            raised_error = error

    protocol.close(time.monotonic() + 1)
    assert raised_error is interruption
    assert raised_error.__cause__ is ordinary_error
    assert raised_error.__context__ is None
    assert wrapper.__cause__ is interruption


def test_owner_start_errors_prioritize_later_interruption() -> None:
    protocol = _protocol_with_streams(
        _BlockingReader(),
        _BlockingWriter(block_after=100),
        generation_nonce="owner-start-interruption-priority",
    )
    ordinary_error = RuntimeError("first ordinary owner start error")

    def make_interruption() -> KeyboardInterrupt:
        try:
            raise KeyboardInterrupt("later owner start interruption")
        except KeyboardInterrupt as error:
            return error

    interruption = make_interruption()
    try:
        with protocol._owner_handle_lock:
            protocol._owner_start_errors = {
                "reader": ordinary_error,
                "writer": interruption,
            }

        with pytest.raises(
            KeyboardInterrupt,
            match="later owner start interruption",
        ) as raised:
            protocol._raise_owner_start_error()

        assert raised.value is interruption
        assert raised.value.__cause__ is ordinary_error
        traceback_names: list[str] = []
        current = raised.value.__traceback__
        while current is not None:
            traceback_names.append(current.tb_frame.f_code.co_name)
            current = current.tb_next
        assert "make_interruption" in traceback_names
    finally:
        with protocol._owner_handle_lock:
            protocol._owner_start_errors.clear()
        protocol.close(time.monotonic() + 1)


def test_constructor_rejects_expired_deadline_before_starting_owner_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starts: list[str] = []

    def reject_start(thread: threading.Thread) -> None:
        starts.append(thread.name)
        raise AssertionError("owner thread started after the caller deadline")

    monkeypatch.setattr(threading.Thread, "start", reject_start)
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)

    with pytest.raises(TimeoutError, match="thread-start deadline"):
        LspProtocol(
            reader,
            writer,
            "expired-thread-start-deadline",
            fatal_callback=lambda _reason: None,
            _startup_deadline=time.monotonic() - 1,
        )

    assert starts == []
    assert reader.closed is True
    assert writer.closed is True


def test_constructor_accepts_live_sub_half_second_owner_start_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_monotonic = time.monotonic
    now = real_monotonic()
    startup_deadline = now + 0.4
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    protocol: LspProtocol | None = None

    with monkeypatch.context() as patch:
        patch.setattr(lsp_protocol.time, "monotonic", lambda: now)
        protocol = LspProtocol(
            reader,
            writer,
            "live-sub-half-second-start-deadline",
            fatal_callback=lambda _reason: None,
            _startup_deadline=startup_deadline,
        )
        with protocol._owner_handle_lock:
            registrations = dict(protocol._owner_registration_monotonic)

    try:
        assert set(registrations) == {"reader", "writer"}
        assert all(registered <= startup_deadline for registered in registrations.values())
        assert protocol.stdout_reader_owner == protocol.reader_thread.ident
        assert protocol.stdin_writer_owner == protocol.writer_thread.ident
    finally:
        if protocol is not None:
            protocol.close(real_monotonic() + 1)


_MONOTONIC_TICK = 0.0156


def test_constructor_rechecks_budget_before_each_owner_thread_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_start = threading.Thread.start
    starts: list[str] = []
    startup_deadline = time.monotonic() + 0.05
    raised_error: BaseException | None = None

    def delay_writer_start(thread: threading.Thread) -> None:
        starts.append(thread.name)
        if thread.name.startswith("lsp-stdin-"):
            remaining = startup_deadline - time.monotonic()
            if remaining > -_MONOTONIC_TICK:
                # `time.monotonic()` advances in 15.6 ms steps on Windows, so
                # overshoot the deadline by several ticks or the recheck still
                # reads a time inside the budget.
                time.sleep(remaining + _MONOTONIC_TICK * 4)
        real_start(thread)

    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=100)
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", delay_writer_start)
        try:
            LspProtocol(
                reader,
                writer,
                "per-owner-start-budget",
                fatal_callback=lambda _reason: None,
                _startup_deadline=startup_deadline,
            )
        except BaseException as error:
            raised_error = error

    assert raised_error is not None, starts
    pending: list[BaseException] = [raised_error]
    errors = _reachable_errors(pending[0])
    retained = _first_retained_cleanup(errors)
    if retained is not None:
        retained.protocol.close(time.monotonic() + 1)

    assert starts == ["lsp-stdin-per-owner-start-budget"]
    assert any(
        isinstance(error, TimeoutError) and "thread-start deadline" in str(error)
        for error in errors
    )


def test_constructor_failure_retains_blocked_owner_for_cleanup_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _UninterruptibleReader()
    writer = _BlockingWriter(block_after=100)

    def fail_wait(
        _event: threading.Event, _owner_name: str, _deadline: float
    ) -> None:
        raise RuntimeError("startup wait failed")

    monkeypatch.setattr(LspProtocol, "_wait_owner_started", staticmethod(fail_wait))
    monkeypatch.setattr(lsp_protocol, "OWNER_JOIN_SECONDS", 0.02)
    try:
        with pytest.raises(RuntimeError) as raised:
            _protocol_with_streams(
                reader,
                writer,
                generation_nonce="constructor-blocked-owner",
            )

        error = raised.value
        assert isinstance(error, lsp_protocol.ProtocolStartupCleanupError)
        protocol = error.protocol
        assert protocol.reader_thread.is_alive()
    finally:
        reader.released.set()
    protocol.close(time.monotonic() + 1)
    assert not protocol.reader_thread.is_alive()
    assert not protocol.writer_thread.is_alive()


class _UninterruptibleReader(_BlockingReader):
    def close(self) -> None:
        self.closed = True


def _cancellation_threads() -> tuple[threading.Thread, ...]:
    return tuple(
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("lsp-cancel-")
    )


@pytest.mark.parametrize("cleanup", ["close", "finish"])
def test_deadline_cleanup_reports_blocked_protocol_owner(cleanup: str) -> None:
    reader = _UninterruptibleReader()
    protocol = _protocol_with_streams(reader, _BlockingWriter(block_after=100))
    assert reader.started.wait(SHORT_TIMEOUT)

    try:
        with pytest.raises(TimeoutError, match="protocol owner"):
            deadline = time.monotonic() + 0.02
            if cleanup == "close":
                protocol.close(deadline)
            else:
                protocol._finish_io_after_process_exit(deadline)
    finally:
        reader.released.set()
        protocol.close(time.monotonic() + 1)


def test_queued_cancellation_writes_neither_request_nor_cancel() -> None:
    reader, writer, protocol = _protocol_on_blocking_streams(0)
    _hold_the_writer_on_a_frame(protocol, writer)
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
    frames = _frames_before_drain_marker(protocol, writer)
    assert len(frames) == 1
    assert b"test/block" in frames[0]
    assert protocol.pending_count == 0
    protocol.close()


def _signal_when_requester_waits(protocol: LspProtocol, monkeypatch) -> threading.Event:
    """Set once the requester sleeps on its completion, which it then leaves only when completed."""
    waiting = threading.Event()
    wait_for_completion = protocol._wait_for_completion

    def signalling_wait(pending, remaining, cancellation) -> None:
        waiting.set()
        wait_for_completion(pending, remaining, cancellation)

    monkeypatch.setattr(lsp_protocol, "_CANCELLATION_POLL_SECONDS", SHORT_TIMEOUT)
    monkeypatch.setattr(protocol, "_wait_for_completion", signalling_wait)
    return waiting


def test_writer_observes_queued_cancellation_before_requester(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, writer, protocol = _protocol_on_blocking_streams(0)
    _hold_the_writer_on_a_frame(protocol, writer)
    source = CancellationSource()
    requester_waiting = _signal_when_requester_waits(protocol, monkeypatch)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            protocol.request,
            "writer-sees-cancel",
            {},
            deadline=time.monotonic() + SHORT_TIMEOUT,
            cancellation=source.token,
        )
        assert requester_waiting.wait(SHORT_TIMEOUT)
        pending = protocol._pending[protocol.pending_keys[0]]
        source.cancel()
        writer.released.set()
        with pytest.raises(RequestCancelled):
            future.result(timeout=1)

    assert len(_frames_before_drain_marker(protocol, writer)) == 1
    assert pending.terminal_source == "writer"
    assert protocol.fatal is False
    protocol.close()


def test_writer_observes_queued_expiry_without_writing_or_becoming_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, writer, protocol = _protocol_on_blocking_streams(0)
    _hold_the_writer_on_a_frame(protocol, writer)
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

    assert len(_frames_before_drain_marker(protocol, writer)) == 1
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
                    "params": {"diagnostics": "not a list"},
                }
            )
            + _raw_frame({"jsonrpc": "2.0", "method": "$/progress", "params": {}})
        )
        sent.set()

    protocol = fake_server.start(
        handler,
        server_notification_handlers={"$/progress": handled.append},
    )
    assert sent.wait(SHORT_TIMEOUT)
    deadline = time.monotonic() + 1
    while not protocol.fatal and time.monotonic() < deadline:
        time.sleep(0.005)
    _join_io_threads(protocol)
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
    assert writer.started.wait(SHORT_TIMEOUT)
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
    # The cancellation has to win the race with the deadline for the test to
    # measure anything; 0.08 s left the timer competing with the scheduler and
    # a macOS runner timed out instead of cancelling.
    deadline = time.monotonic() + 0.5
    timer.start()

    with pytest.raises(RequestCancelled):
        protocol.request("cancel", {}, deadline=deadline, cancellation=source.token)

    assert time.monotonic() < deadline + 0.2
    assert writer.started.wait(SHORT_TIMEOUT)
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
    reader, writer, protocol = _protocol_on_blocking_streams(0)
    source = CancellationSource()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            protocol.request,
            "sending-cancel",
            {},
            deadline=time.monotonic() + 1,
            cancellation=source.token,
        )
        assert writer.started.wait(SHORT_TIMEOUT)
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
        protocol.request("write-failure", {}, deadline=time.monotonic() + SHORT_TIMEOUT)

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
    reader, writer, protocol = _protocol_on_blocking_streams(0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            protocol.request,
            "close-race",
            {},
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert writer.started.wait(SHORT_TIMEOUT)
        protocol.close()
        with pytest.raises(ProtocolViolation, match="closed"):
            future.result(timeout=1)
    assert protocol.pending_count == 0
    assert not protocol.reader_thread.is_alive()
    assert not protocol.writer_thread.is_alive()


# Closing from the reader handler skips joining the reader — it is the current
# thread — and still joins the writer. The default budget for that join is one
# second, and on 2026-08-22 the Windows/3.13 shard of CI run 32548995167 did not
# make it: close() raised, `handler_returned` was never set, and the test spent
# its full wait reporting `assert False` with no cause. The deadline belongs to
# the caller, so the test names one that outlasts a loaded runner.
_CLOSE_FROM_HANDLER_SECONDS = 30.0


# The notification that triggers the handler is sent from this thread, after the
# protocol exists. `FakeLspServer.start` starts the peer-handler thread before it
# returns, so a peer handler that sent `$/progress` opened a window: the reader
# could dispatch it while the assignment `protocol = fake_server.start(...)` had
# not bound the name, and the handler's closure raised
# `NameError: free variable 'protocol' referenced before assignment`. That is what
# the macOS job 97818285097 of CI run 32852933106 hit — macOS only because it
# scheduled the reader early enough. Binding first and sending after closes the
# window at its source; nothing here waits on a timer for the race to pass.
def test_close_from_reader_handler_does_not_deadlock(fake_server: FakeLspServer) -> None:
    handler_returned = threading.Event()
    handler_failure: list[BaseException] = []

    def notification_handler(_params: object) -> None:
        try:
            protocol.close(time.monotonic() + _CLOSE_FROM_HANDLER_SECONDS)
        except BaseException as error:  # noqa: BLE001 - a hang must say why
            handler_failure.append(error)
        finally:
            # Set in `finally`, so a real deadlock never reaches it and this
            # stays the liveness test it was; only the diagnosis is new.
            handler_returned.set()

    protocol = fake_server.start(
        server_notification_handlers={"$/progress": notification_handler},
    )
    fake_server.peers[-1].send({"jsonrpc": "2.0", "method": "$/progress", "params": {}})
    assert handler_returned.wait(LONG_TIMEOUT), "close() never returned from the reader handler"
    assert not handler_failure, f"close() raised {handler_failure[0]!r}"
    protocol.reader_thread.join(LONG_TIMEOUT)
    assert not protocol.reader_thread.is_alive()
    assert not protocol.writer_thread.is_alive()


def test_duplicate_cancel_finalization_emits_only_one_cancel_frame() -> None:
    reader, writer, protocol = _protocol_on_blocking_streams()
    source = CancellationSource()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            protocol.request,
            "duplicate-cancel",
            {},
            deadline=time.monotonic() + 1,
            cancellation=source.token,
        )
        _await_frames(writer, 1)
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
    result = protocol.request(method, {}, deadline=time.monotonic() + SHORT_TIMEOUT)
    assert len(result) == MAX_LOCATIONS
    with pytest.raises(ProtocolViolation, match="location"):
        protocol.request(method, {}, deadline=time.monotonic() + SHORT_TIMEOUT)


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
        "textDocument/documentSymbol", {}, deadline=time.monotonic() + SHORT_TIMEOUT
    )
    assert sum(1 + len(root["children"]) for root in accepted) == MAX_LOCATIONS
    with pytest.raises(ProtocolViolation, match="location"):
        protocol.request("textDocument/documentSymbol", {}, deadline=time.monotonic() + SHORT_TIMEOUT)


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
    reader, writer, protocol = _protocol_on_blocking_streams()
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
    reader, writer, protocol = _protocol_on_blocking_streams()
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
        assert future.result(timeout=SHORT_TIMEOUT) == "response-won"
    protocol.close()


def test_fatal_committed_before_cancellation_remains_caller_outcome() -> None:
    reader, writer, protocol = _protocol_on_blocking_streams()
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
    reader, writer, protocol = _protocol_on_blocking_streams()
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
    reader, writer, protocol = _protocol_on_blocking_streams()
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
    _await_frames(writer, 2)
    assert _cancel_frames(writer) == 1
    protocol.close()


def test_equal_cancellation_and_response_timestamps_prefer_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, writer, protocol = _protocol_on_blocking_streams()
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
    reader, writer, protocol = _protocol_on_blocking_streams()
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
    reader, writer, protocol = _protocol_on_blocking_streams()
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
    reader, writer, protocol = _protocol_on_blocking_streams()
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
        _await_frames(writer, MAX_PENDING_REQUESTS)
        assert len(writer.frames) == MAX_PENDING_REQUESTS
        _cancel_all_and_expect(sources, futures, timeout=1)

    assert protocol.pending_count == MAX_PENDING_REQUESTS
    assert protocol._next_request_id == MAX_PENDING_REQUESTS + 1
    with pytest.raises(PendingRequestLimitExceeded):
        _request(protocol)

    _dispatch_late_responses(protocol, MAX_PENDING_REQUESTS)
    assert protocol.pending_count == 0
    assert protocol.pending_keys == ()
    protocol.close()


def test_ordinary_writes_cannot_consume_reserved_control_capacity() -> None:
    reader, writer, protocol = _protocol_on_blocking_streams(0)
    _hold_the_writer_on_a_frame(protocol, writer)
    ordinary_limit = lsp_protocol.MAX_QUEUED_WRITES - MAX_PENDING_REQUESTS

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
        _await_frames(writer, MAX_PENDING_REQUESTS)
        assert len(writer.frames) == MAX_PENDING_REQUESTS
        protocol._write_message({"jsonrpc": "2.0", "method": "test/block"})
        assert writer.started.wait(SHORT_TIMEOUT)
        ordinary_limit = lsp_protocol.MAX_QUEUED_WRITES - MAX_PENDING_REQUESTS
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
        _cancel_all_and_expect(sources, futures, timeout=0.5)
        assert protocol._control_queued == MAX_PENDING_REQUESTS
        assert protocol._write_queue.qsize() == lsp_protocol.MAX_QUEUED_WRITES
        writer.released.set()

    _await_cancel_frames(writer, 32)
    assert _cancel_frames(writer) == 32
    assert protocol.fatal is False
    protocol.close()


def test_request_ids_are_not_reused_after_drain_release() -> None:
    reader, writer, protocol = _protocol_on_blocking_streams()
    source = CancellationSource()

    with ThreadPoolExecutor(max_workers=1) as pool:
        cancelled = pool.submit(
            protocol.request,
            "first",
            {},
            deadline=time.monotonic() + SHORT_TIMEOUT,
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
        assert fresh.result(timeout=SHORT_TIMEOUT) == "fresh"
    assert protocol._next_request_id == 3
    protocol.close()


def test_late_peer_cancellation_drains_without_changing_local_outcome() -> None:
    reader, writer, protocol = _protocol_on_blocking_streams()
    source = CancellationSource()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            protocol.request,
            "late-peer-cancel",
            {},
            deadline=time.monotonic() + 1,
            cancellation=source.token,
        )
        _await_frames(writer, 1)
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
    reader, writer, protocol = _protocol_on_blocking_streams()
    source = CancellationSource()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            protocol.request,
            "close-drain",
            {},
            deadline=time.monotonic() + SHORT_TIMEOUT,
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
    reader, writer, protocol = _protocol_on_blocking_streams()
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
    assert protocol.next_drain_deadline() == now - 1
    assert protocol.pending_count == 3
    with pytest.raises(TypeError):
        protocol.expired_drain_keys(True)
    with pytest.raises(ValueError):
        protocol.expired_drain_keys(float("inf"))
    protocol.close()


def test_finish_after_process_exit_completes_pending_immediately_and_clears_it() -> None:
    reader, writer, protocol = _protocol_on_blocking_streams()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            protocol.request,
            "pending-at-exit",
            {},
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert writer.started.wait(1) is False
        _await_frames(writer, 1)
        assert writer.frames

        protocol._finish_io_after_process_exit(time.monotonic() + 1)

        with pytest.raises(ProtocolViolation, match="closed"):
            pending.result(timeout=0.2)
    assert protocol.pending_count == 0
    assert not protocol.reader_thread.is_alive()
    assert not protocol.writer_thread.is_alive()


def test_protocol_cleanup_timeout_is_retryable_after_blocked_owner_releases() -> None:
    reader = _UninterruptibleReader()
    protocol = _protocol_with_streams(reader, _BlockingWriter(block_after=100))
    assert reader.started.wait(SHORT_TIMEOUT)

    with pytest.raises(TimeoutError, match="protocol owner"):
        protocol.close(time.monotonic() + 0.02)

    assert protocol.reader_thread.is_alive()
    reader.released.set()
    protocol.close(time.monotonic() + 1)
    assert not protocol.reader_thread.is_alive()
    assert not protocol.writer_thread.is_alive()


def test_protocol_close_clears_all_queued_write_ownership_after_join() -> None:
    reader = _BlockingReader()
    writer = _BlockingWriter(block_after=0)
    protocol = _protocol_with_streams(reader, writer)
    protocol._write_message({"jsonrpc": "2.0", "method": "queued/one"})
    assert writer.started.wait(SHORT_TIMEOUT)
    protocol._write_message({"jsonrpc": "2.0", "method": "queued/two"})
    protocol._write_message({"jsonrpc": "2.0", "method": "queued/three"})

    protocol.close(time.monotonic() + 1)

    assert protocol._write_tasks == []
    assert protocol._ordinary_queued == 0
    assert protocol._control_queued == 0
    assert protocol._write_queue.empty()
    assert not protocol.reader_thread.is_alive()
    assert not protocol.writer_thread.is_alive()


def test_process_cleanup_stop_wakes_pending_before_blocked_owners_release() -> None:
    reader = _UninterruptibleReader()
    writer = _BlockingWriter(block_after=0)
    protocol = _protocol_with_streams(reader, writer)
    assert reader.started.wait(SHORT_TIMEOUT)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            protocol.request,
            "blocked-during-cleanup",
            {},
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert writer.started.wait(SHORT_TIMEOUT)
        protocol._stop_io_for_process_cleanup()

        with pytest.raises(ProtocolViolation, match="closed"):
            pending.result(timeout=0.2)
        assert protocol.pending_count == 0
        assert protocol.reader_thread.is_alive()
        assert protocol.writer_thread.is_alive()

        reader.released.set()
        writer.released.set()
    protocol._finish_io_after_process_exit(time.monotonic() + 1)
    assert not protocol.reader_thread.is_alive()
    assert not protocol.writer_thread.is_alive()


@pytest.mark.skipif(os.name != "nt", reason="Windows synchronous I/O interruption")
def test_windows_owner_handles_are_retained_cancelled_and_closed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicated = iter((101, 202))
    cancelled: list[int] = []
    closed: list[int] = []

    class FakeKernel32:
        @staticmethod
        def OpenThread(*_args: object) -> int:
            raise AssertionError("cleanup reopened a thread by native ID")

        @staticmethod
        def CancelSynchronousIo(handle: int) -> bool:
            cancelled.append(handle)
            return True

        @staticmethod
        def CloseHandle(handle: int) -> bool:
            closed.append(handle)
            return True

    reader = _BlockingReader()
    monkeypatch.setattr(
        lsp_protocol,
        "duplicate_current_thread_handle",
        lambda: next(duplicated),
    )
    monkeypatch.setattr(lsp_protocol, "KERNEL32", FakeKernel32())
    protocol = _protocol_with_streams(reader, _BlockingWriter(block_after=100))
    assert reader.started.wait(SHORT_TIMEOUT)

    assert {
        protocol._reader_os_handle,
        protocol._writer_os_handle,
    } == {101, 202}

    protocol.close(time.monotonic() + 1)

    assert sorted(cancelled) == [101, 202]
    assert sorted(closed) == [101, 202]
    assert protocol._reader_os_handle is None
    assert protocol._writer_os_handle is None
    assert not protocol.reader_thread.is_alive()
    assert not protocol.writer_thread.is_alive()


class _CrtPipeReader:
    """A Windows pipe as the C runtime serves it: close waits for a read in progress.

    The fake records a close that arrives while its owner is inside `read` instead of
    blocking, which on Windows is the hang of CI run 36312497314. See
    docs/research/2026-09-27-a-reader-is-cancelled-until-it-leaves.md.
    """

    def __init__(self) -> None:
        self._read_fd, self._write_fd = os.pipe()
        self.inside_read = threading.Event()
        self.released = threading.Event()
        self.closed_during_read = False

    def fileno(self) -> int:
        return self._read_fd

    def read(self, _size: int = -1) -> bytes:
        self.inside_read.set()
        self.released.wait(LONG_TIMEOUT)
        self.inside_read.clear()
        return b""

    def close(self) -> None:
        self.closed_during_read = self.closed_during_read or self.inside_read.is_set()
        self.released.set()
        os.close(self._read_fd)
        os.close(self._write_fd)


class _LateCancelProtocol(LspProtocol):
    """CancelSynchronousIo at the platform boundary: the first cancel finds no read yet."""

    reader_cancels = 0

    def _cancel_owner_io(self, owner: threading.Thread) -> None:
        if owner is not self.reader_thread:
            return
        self.reader_cancels += 1
        if self.reader_cancels > 1:
            self._reader.released.set()


def test_a_lost_cancel_is_repeated_and_a_read_stream_is_never_closed_under_its_reader(monkeypatch) -> None:
    monkeypatch.setattr(lsp_protocol, "CLOSE_WAITS_FOR_A_READ", True, raising=False)
    monkeypatch.setattr(lsp_protocol, "PIPES_HAVE_NO_SELECT", True, raising=False)
    reader = _CrtPipeReader()
    protocol = _LateCancelProtocol(
        reader, _BlockingWriter(block_after=100), "crt-pipe", fatal_callback=lambda _reason: None  # type: ignore[arg-type]
    )
    assert reader.inside_read.wait(LONG_TIMEOUT)

    protocol.close()

    assert not reader.closed_during_read
    assert protocol.reader_cancels > 1
    assert not protocol.reader_thread.is_alive()


def test_a_peer_closes_cleanly_after_its_client_left() -> None:
    """A send that met a departed client leaves nothing for close to send again.

    Under load the fatal-callback test's handler sent its second frame after teardown
    had closed the client; the buffered peer writer kept it and raised BrokenPipeError
    from close. docs/research/2026-09-27-a-reader-is-cancelled-until-it-leaves.md
    """
    client, server = socket.socketpair()
    peer = FakeLspPeer(server)
    client.close()
    assert _send_until_the_client_is_gone(peer), "the departed client was never reported"
    peer.close()


def _send_until_the_client_is_gone(peer: FakeLspPeer) -> bool:
    """Send until the OS reports the departed client; True once it does.

    On Linux the first send to a closed socketpair end fails; on Windows the pair is
    TCP over loopback, the first send is buffered and a later one meets the reset
    (CI run 36339197300). The test is about what close does after that failure, so
    it waits for the failure on every platform, bounded by LONG_TIMEOUT.
    """
    deadline = time.monotonic() + LONG_TIMEOUT
    while time.monotonic() < deadline:
        try:
            peer.send_raw(b"Worse\r\n\r\n")
        except OSError:
            return True
    return False


def test_only_a_pipe_is_left_to_its_windows_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """A socket read is woken by closing its stream; a pipe read is not (run 36334525135).

    Holding a socket stream for its owner left Windows readers blocked: Winsock's
    shutdown disallows only later receives, and the socket stays open while its
    makefile does, so every socket-backed close timed out.
    """
    monkeypatch.setattr(lsp_protocol, "CLOSE_WAITS_FOR_A_READ", True)
    left, right = socket.socketpair()
    read_fd, write_fd = os.pipe()
    socket_stream = left.makefile("rb")
    pipe_stream = os.fdopen(read_fd, "rb", buffering=0)
    try:
        assert (lsp_protocol.fd_stream_held(socket_stream), lsp_protocol.fd_stream_held(pipe_stream)) == (False, True)
    finally:
        socket_stream.close()
        pipe_stream.close()
        os.close(write_fd)
        left.close()
        right.close()


@pytest.mark.parametrize('name', [
    'ProtocolViolation', 'FrameTooLarge', 'JsonRpcFrameReader', 'encode_frame', 'json_depth',
])
def test_public_wire_alias_keeps_object_and_pickle_identity(name: str) -> None:
    import pickle

    from llm_wiki import lsp_wire

    original = getattr(lsp_protocol, name)
    assert original is getattr(lsp_wire, name)
    assert original.__module__ == 'lsp_protocol'
    assert pickle.loads(pickle.dumps(original)) is original
