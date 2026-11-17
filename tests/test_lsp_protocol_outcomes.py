"""How the LSP transport settles a response, a cancellation, a deadline and a fatal error."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import lsp_protocol
import pytest
from lsp_protocol import (
    CANCEL_DRAIN_GRACE_SECONDS,
    MAX_PENDING_REQUESTS,
    CancellationSource,
    PendingRequest,
    PendingRequestLimitExceeded,
    ProtocolViolation,
    RequestCancelled,
)

from tests.lsp_protocol_helpers import (
    _await_cancel_frames,
    _await_frames,
    _BlockingReader,
    _BlockingWriter,
    _cancel_all_and_expect,
    _cancel_frames,
    _cancellation_threads,
    _close_path,  # noqa: F401 - a fixture this module uses
    _dispatch_late_responses,
    _hold_the_writer_on_a_frame,
    _protocol_on_blocking_streams,
    _protocol_with_streams,
    _request,
    _UninterruptibleReader,
)
from tests.slow_machine import SHORT_TIMEOUT


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
