"""Strict and bounded LSP JSON-RPC framing and request coordination."""

from __future__ import annotations

import json
import math
import os
import queue
import select
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, BinaryIO

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _ERROR_NOT_FOUND = 1168
    _DUPLICATE_SAME_ACCESS = 0x00000002
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _KERNEL32.GetCurrentProcess.argtypes = ()
    _KERNEL32.GetCurrentProcess.restype = wintypes.HANDLE
    _KERNEL32.GetCurrentThread.argtypes = ()
    _KERNEL32.GetCurrentThread.restype = wintypes.HANDLE
    _KERNEL32.DuplicateHandle.argtypes = (
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    _KERNEL32.DuplicateHandle.restype = wintypes.BOOL
    _KERNEL32.CancelSynchronousIo.argtypes = (wintypes.HANDLE,)
    _KERNEL32.CancelSynchronousIo.restype = wintypes.BOOL
    _KERNEL32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _KERNEL32.CloseHandle.restype = wintypes.BOOL

MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_HEADER_BYTES = 8 * 1024
MAX_PENDING_REQUESTS = 32
MAX_LOCATIONS = 10_000
MAX_DIAGNOSTICS = 10_000
MAX_HOVER_BYTES = 256 * 1024
MAX_JSON_DEPTH = 64
CANCEL_DRAIN_GRACE_SECONDS = 2.0
METHOD_NOT_FOUND = -32601

SERVER_REQUESTS = frozenset(
    {
        "client/registerCapability",
        "client/unregisterCapability",
        "window/workDoneProgress/create",
        "workspace/configuration",
    }
)
SERVER_NOTIFICATIONS = frozenset(
    {
        "$/progress",
        "pyright/beginProgress",
        "pyright/endProgress",
        "pyright/reportProgress",
        "textDocument/publishDiagnostics",
    }
)

_FLAT_SEMANTIC_RESULT_METHODS = frozenset(
    {
        "callHierarchy/incomingCalls",
        "callHierarchy/outgoingCalls",
        "textDocument/declaration",
        "textDocument/definition",
        "textDocument/implementation",
        "textDocument/prepareCallHierarchy",
        "textDocument/references",
        "textDocument/typeDefinition",
        "workspace/symbol",
    }
)
_JSON_RPC_INTEGER_MIN = -(2**31)
_JSON_RPC_INTEGER_MAX = 2**31 - 1
_TOMBSTONE_LIMIT = MAX_PENDING_REQUESTS * 4
_CANCELLATION_POLL_SECONDS = 0.01
_UNKNOWN_NOTIFICATION_WARNING = "dropped unknown server notification"
_MAX_JSON_VALUES = MAX_FRAME_BYTES // 2
_MAX_QUEUED_WRITES = MAX_PENDING_REQUESTS * 4
_MAX_ORDINARY_WRITES = _MAX_QUEUED_WRITES - MAX_PENDING_REQUESTS
_INTERNAL_WRITE_SECONDS = 1.0
_OWNER_JOIN_SECONDS = 1.0


class ProtocolViolation(RuntimeError):
    """A peer sent data outside the bounded LSP protocol contract."""


class _LocalRequestViolation(ProtocolViolation):
    """A caller request failed validation before transport ownership."""


class RequestCancelled(RuntimeError):
    """The caller cancelled an active LSP request."""


class PendingRequestLimitExceeded(RuntimeError):
    """The connection already has the maximum number of active requests."""


class _ProtocolStartupCleanupError(RuntimeError):
    """Protocol construction failed while an I/O owner remains retryable."""

    def __init__(
        self,
        protocol: LspProtocol,
        errors: tuple[BaseException, ...],
    ) -> None:
        super().__init__("LSP protocol startup cleanup retains ownership")
        self.protocol = protocol
        self.errors = errors


@dataclass(slots=True)
class _CancellationState:
    lock: threading.Lock
    event: threading.Event
    cancelled_at: float | None = None


class CancellationToken:
    """Read-only, sticky cancellation state shared across threads."""

    __slots__ = ("_state",)

    def __init__(self, state: _CancellationState) -> None:
        self._state = state

    @property
    def cancelled_at(self) -> float | None:
        with self._state.lock:
            return self._state.cancelled_at

    def is_cancelled(self) -> bool:
        return self._state.event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._state.event.wait(timeout)


class CancellationSource:
    """Single authority for cancelling one cooperative operation scope."""

    __slots__ = ("_state", "_token")

    def __init__(self) -> None:
        self._state = _CancellationState(threading.Lock(), threading.Event())
        self._token = CancellationToken(self._state)

    @property
    def token(self) -> CancellationToken:
        return self._token

    def cancel(self) -> bool:
        with self._state.lock:
            if self._state.cancelled_at is not None:
                return False
            self._state.cancelled_at = time.monotonic()
            self._state.event.set()
            return True


class JsonRpcResponseError(RuntimeError):
    """A valid JSON-RPC response reported a server error."""

    def __init__(self, error: JsonRpcError) -> None:
        super().__init__(f"JSON-RPC error {error.code}: {error.message}")
        self.error = error


@dataclass(slots=True)
class PendingRequest:
    request_id: int
    method: str
    generation_nonce: str
    deadline: float
    completed: threading.Event
    cancellation: CancellationToken | None = None
    result: object | None = None
    error: JsonRpcError | None = None
    write_phase: str = "queued"
    responded_at: float | None = None
    terminal: str | None = None
    terminal_at: float | None = None
    terminal_source: str | None = None
    terminal_error: ProtocolViolation | None = None
    drain_deadline: float | None = None
    cancel_enqueued: bool = False


@dataclass(frozen=True, slots=True)
class JsonRpcError:
    code: int
    message: str
    data: object | None


@dataclass(slots=True, eq=False)
class _WriteTask:
    frame: bytes
    deadline: float
    completed: threading.Event
    request_key: tuple[str, int] | None = None
    best_effort: bool = False
    control: bool = False
    error: BaseException | None = None


class _OwnedReader:
    def __init__(self, stream: BinaryIO, stopped: threading.Event) -> None:
        self._stream = stream
        self._stopped = stopped

    def read(self, size: int = -1) -> bytes:
        if os.name == "nt":
            return self._stream.read(size)
        try:
            descriptor = self._stream.fileno()
        except (AttributeError, OSError):
            return self._stream.read(size)
        while not self._stopped.is_set():
            try:
                readable, _, _ = select.select([descriptor], [], [], 0.05)
            except (OSError, ValueError):
                return b""
            if readable:
                try:
                    return os.read(descriptor, size)
                except OSError:
                    return b""
        return b""


def _strict_string_size(value: str) -> int:
    try:
        return len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ProtocolViolation("JSON strings must not contain lone surrogates") from exc


def json_depth(value: object) -> int:
    """Validate a strict JSON value and return its bounded container depth."""
    maximum = 0
    values_seen = 0
    string_bytes = 0
    active_containers: set[int] = set()
    stack: list[tuple[object, int, bool]] = [(value, 0, False)]
    while stack:
        current, parent_depth, exiting = stack.pop()
        if exiting:
            active_containers.remove(id(current))
            continue
        values_seen += 1
        if values_seen > _MAX_JSON_VALUES:
            raise ProtocolViolation("JSON value count exceeds the frame bound")
        if isinstance(current, str):
            string_bytes += _strict_string_size(current)
            if string_bytes > MAX_FRAME_BYTES:
                raise ProtocolViolation("JSON strings exceed the frame bound")
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise ProtocolViolation("JSON numbers must be finite")
            continue
        if current is None or isinstance(current, (bool, int)):
            continue
        if not isinstance(current, (dict, list)):
            raise ProtocolViolation("value is not a strict JSON type")

        identity = id(current)
        if identity in active_containers:
            raise ProtocolViolation("cyclic JSON values are not supported")
        active_containers.add(identity)
        depth = parent_depth + 1
        maximum = max(maximum, depth)
        stack.append((current, depth, True))
        if isinstance(current, dict):
            for key, child in current.items():
                if isinstance(key, float) and not math.isfinite(key):
                    raise ProtocolViolation("JSON numbers must be finite")
                if not isinstance(key, str):
                    raise ProtocolViolation("JSON object keys must be strings")
                string_bytes += _strict_string_size(key)
                if string_bytes > MAX_FRAME_BYTES:
                    raise ProtocolViolation("JSON strings exceed the frame bound")
                stack.append((child, depth, False))
        else:
            for child in reversed(current):
                stack.append((child, depth, False))
    return maximum


def _valid_id(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return _JSON_RPC_INTEGER_MIN <= value <= _JSON_RPC_INTEGER_MAX
    return isinstance(value, str)


def _validate_error(value: object) -> None:
    if not isinstance(value, dict):
        raise ProtocolViolation("response error must be an object")
    if not set(value) <= {"code", "message", "data"} or not {"code", "message"} <= set(
        value
    ):
        raise ProtocolViolation("response error has invalid shape")
    code = value["code"]
    if (
        isinstance(code, bool)
        or not isinstance(code, int)
        or not _JSON_RPC_INTEGER_MIN <= code <= _JSON_RPC_INTEGER_MAX
    ):
        raise ProtocolViolation("response error code is invalid")
    if not isinstance(value["message"], str):
        raise ProtocolViolation("response error message is invalid")


def _validate_message(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolViolation("JSON-RPC batches and scalar messages are not supported")
    if value.get("jsonrpc") != "2.0":
        raise ProtocolViolation("JSON-RPC version must be 2.0")

    if "method" in value:
        if not set(value) <= {"jsonrpc", "id", "method", "params"}:
            raise ProtocolViolation("request or notification has invalid shape")
        if not isinstance(value["method"], str) or not value["method"]:
            raise ProtocolViolation("method must be a non-empty string")
        if "id" in value and not _valid_id(value["id"]):
            raise ProtocolViolation("request ID is invalid")
        if "params" in value and not isinstance(value["params"], (dict, list)):
            raise ProtocolViolation("params must be an object or array")
        return value

    if not set(value) <= {"jsonrpc", "id", "result", "error"}:
        raise ProtocolViolation("response has invalid shape")
    if "id" not in value or not _valid_id(value["id"]):
        raise ProtocolViolation("response ID is invalid")
    if ("result" in value) == ("error" in value):
        raise ProtocolViolation("response must contain exactly one of result or error")
    if "error" in value:
        _validate_error(value["error"])
    return value


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("duplicate JSON object member")
        value[name] = item
    return value


def _decode_body(body: bytes) -> dict[str, Any]:
    try:
        text = body.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ProtocolViolation("message body is not strict UTF-8 JSON") from exc
    if json_depth(value) > MAX_JSON_DEPTH:
        raise ProtocolViolation("JSON depth exceeds 64")
    return _validate_message(value)


class JsonRpcFrameReader:
    """Read strict Content-Length framed JSON-RPC messages from one binary stream."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream

    def read(self) -> dict[str, Any]:
        header = bytearray()
        while not header.endswith(b"\r\n\r\n"):
            octet = self._stream.read(1)
            if not octet:
                raise ProtocolViolation("unexpected EOF in LSP header")
            header.extend(octet)
            if len(header) > MAX_HEADER_BYTES:
                raise ProtocolViolation("LSP header exceeds 8 KiB")

        headers: dict[str, str] = {}
        try:
            lines = bytes(header[:-4]).decode("ascii", errors="strict").split("\r\n")
        except UnicodeDecodeError as exc:
            raise ProtocolViolation("LSP header must be ASCII") from exc
        for line in lines:
            if ": " not in line:
                raise ProtocolViolation("LSP header field is malformed")
            name, field_value = line.split(": ", 1)
            lowered = name.lower()
            if not name or not field_value or lowered in headers:
                raise ProtocolViolation("LSP header field is missing or duplicated")
            headers[lowered] = field_value

        length_text = headers.get("content-length")
        if length_text is None or not length_text.isascii() or not length_text.isdecimal():
            raise ProtocolViolation("Content-Length is missing or invalid")
        if len(length_text) > len(str(MAX_FRAME_BYTES)):
            raise ProtocolViolation("Content-Length exceeds the frame limit")
        length = int(length_text)
        if length > MAX_FRAME_BYTES:
            raise ProtocolViolation("LSP frame exceeds 8 MiB")

        content_type = headers.get("content-type")
        if content_type is not None:
            parts = [part.strip() for part in content_type.lower().split(";")]
            if parts[0] != "application/vscode-jsonrpc":
                raise ProtocolViolation("unsupported LSP content type")
            parameters = [part for part in parts[1:] if part]
            if parameters not in ([], ["charset=utf-8"], ["charset=utf8"]):
                raise ProtocolViolation("unsupported LSP charset")

        body = bytearray()
        while len(body) < length:
            chunk = self._stream.read(length - len(body))
            if not chunk:
                raise ProtocolViolation("unexpected EOF in LSP body")
            body.extend(chunk)
        return _decode_body(bytes(body))


def encode_frame(message: object) -> bytes:
    """Encode one strict, canonical, byte-counted LSP JSON-RPC frame."""
    if json_depth(message) > MAX_JSON_DEPTH:
        raise ProtocolViolation("JSON depth exceeds 64")
    _validate_message(message)
    try:
        body = json.dumps(
            message,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProtocolViolation("message cannot be encoded as strict JSON") from exc
    if len(body) > MAX_FRAME_BYTES:
        raise ProtocolViolation("LSP frame exceeds 8 MiB")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def _interruption_in_chain(
    error: BaseException,
) -> KeyboardInterrupt | SystemExit | None:
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (KeyboardInterrupt, SystemExit)):
            return current
        if current.__context__ is not None:
            pending.append(current.__context__)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return None


def _exception_reaches(error: BaseException | None, target: BaseException) -> bool:
    pending = [error] if error is not None else []
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is target:
            return True
        if id(current) in seen:
            continue
        seen.add(id(current))
        if current.__context__ is not None:
            pending.append(current.__context__)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return False


def _raise_collected_errors(errors: Sequence[BaseException]) -> None:
    if not errors:
        return
    source: BaseException | None = None
    interruption: KeyboardInterrupt | SystemExit | None = None
    for error in errors:
        interruption = _interruption_in_chain(error)
        if interruption is not None:
            source = error
            break
    if interruption is None:
        raise errors[0]
    secondary = next(
        (
            error
            for error in errors
            if error is not source and error is not interruption
        ),
        None,
    )
    if secondary is None and source is not interruption:
        secondary = source
    if secondary is not None:
        pending = [secondary]
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            cause = current.__cause__
            if cause is interruption:
                current.__cause__ = None
            elif cause is not None:
                pending.append(cause)
            context = current.__context__
            if context is interruption or context is current.__cause__:
                current.__context__ = None
            elif context is not None:
                pending.append(context)
        if _exception_reaches(secondary, interruption):
            secondary = None
    try:
        if secondary is not None:
            raise interruption.with_traceback(
                interruption.__traceback__
            ) from secondary
        raise interruption.with_traceback(interruption.__traceback__)
    except (KeyboardInterrupt, SystemExit) as raised:
        if raised is not interruption:
            raise
        if _exception_reaches(interruption.__cause__, interruption):
            interruption.__cause__ = None
        if _exception_reaches(interruption.__context__, interruption):
            interruption.__context__ = None
        if interruption.__cause__ is not None:
            interruption.__context__ = None
        raise


class LspProtocol:
    """Coordinate bounded concurrent requests over one LSP process generation."""

    def __init__(
        self,
        reader: BinaryIO,
        writer: BinaryIO,
        generation_nonce: str,
        *,
        fatal_callback: Callable[[str], None],
        warning_callback: Callable[[str], None] | None = None,
        server_request_handlers: Mapping[str, Callable[[object], object]] | None = None,
        server_notification_handlers: Mapping[str, Callable[[object], None]] | None = None,
        _startup_deadline: float | None = None,
        _drain_wake: threading.Event | None = None,
    ) -> None:
        if not isinstance(generation_nonce, str) or not generation_nonce:
            raise ValueError("generation_nonce must be a non-empty string")
        if not callable(fatal_callback):
            raise TypeError("fatal_callback must be callable")
        if _drain_wake is not None and not isinstance(_drain_wake, threading.Event):
            raise TypeError("_drain_wake must be a threading.Event or None")
        if _startup_deadline is not None:
            if isinstance(_startup_deadline, bool) or not isinstance(
                _startup_deadline, (int, float)
            ):
                raise TypeError("_startup_deadline must be a monotonic timestamp")
            if not math.isfinite(_startup_deadline):
                raise ValueError("_startup_deadline must be finite")
        startup_deadline = (
            time.monotonic() + _OWNER_JOIN_SECONDS
            if _startup_deadline is None
            else float(_startup_deadline)
        )
        self._reader = reader
        self._writer = writer
        self.generation_nonce = generation_nonce
        self._fatal_callback = fatal_callback
        self._warning_callback = warning_callback
        self._drain_wake = _drain_wake
        self._server_request_handlers = dict(server_request_handlers or {})
        self._server_notification_handlers = dict(server_notification_handlers or {})
        self._state_lock = threading.Lock()
        self._pending: dict[tuple[str, int], PendingRequest] = {}
        self._cancelled_keys: set[tuple[str, int]] = set()
        self._cancelled_order: deque[tuple[str, int]] = deque()
        self._responded_keys: set[tuple[str, int]] = set()
        self._responded_order: deque[tuple[str, int]] = deque()
        self._next_request_id = 1
        self._sent_request_sequence = 0
        self._last_sent_request_method: str | None = None
        self._fatal_error: ProtocolViolation | None = None
        self._closed = False
        self._unknown_notification_warned = False
        self._reader_started = threading.Event()
        self._writer_started = threading.Event()
        self._io_stopped = threading.Event()
        self._write_queue: queue.Queue[_WriteTask | None] = queue.Queue(
            maxsize=_MAX_QUEUED_WRITES
        )
        self._ordinary_queued = 0
        self._control_queued = 0
        self._write_tasks: list[_WriteTask] = []
        self._owner_handle_lock = threading.Lock()
        self._reader_os_handle: int | None = None
        self._writer_os_handle: int | None = None
        self._owner_start_errors: dict[str, BaseException] = {}
        self._owner_registration_monotonic: dict[str, float] = {}
        self._owner_interrupt_errors: dict[str, BaseException] = {}
        self._owner_release_errors: dict[str, BaseException] = {}
        self.stdout_reader_owner: int | None = None
        self.stdin_writer_owner: int | None = None
        self.writer_thread = threading.Thread(
            target=self._writer_loop,
            name=f"lsp-stdin-{generation_nonce}",
            daemon=True,
        )
        self.reader_thread = threading.Thread(
            target=self._reader_loop,
            name=f"lsp-stdout-{generation_nonce}",
            daemon=True,
        )
        try:
            self._require_owner_start_budget(startup_deadline)
            self.writer_thread.start()
            self._require_owner_start_budget(startup_deadline)
            self.reader_thread.start()
            self._wait_owner_registration(
                self._writer_started, "writer", startup_deadline
            )
            self._wait_owner_registration(
                self._reader_started, "reader", startup_deadline
            )
            self._raise_owner_start_error()
        except BaseException as startup_error:
            self._io_stopped.set()
            try:
                self._write_queue.put_nowait(None)
            except queue.Full:
                pass
            for owner in (self.reader_thread, self.writer_thread):
                if owner.ident is not None:
                    self._cancel_owner_io(owner)
            self._interrupt_stream(self._reader)
            self._interrupt_stream(self._writer)
            cleanup_errors: list[BaseException] = []
            cleanup_interruption: KeyboardInterrupt | SystemExit | None = None
            for owner in (self.reader_thread, self.writer_thread):
                try:
                    self._join_partially_started_owner(owner, startup_deadline)
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
                    if cleanup_interruption is None and isinstance(
                        cleanup_error, (KeyboardInterrupt, SystemExit)
                    ):
                        cleanup_interruption = cleanup_error
            if cleanup_errors:
                ownership_error = _ProtocolStartupCleanupError(
                    self, tuple(cleanup_errors)
                )
                interruption_source: BaseException | None = None
                if _interruption_in_chain(startup_error) is not None:
                    interruption_source = startup_error
                elif cleanup_interruption is not None:
                    interruption_source = cleanup_interruption
                else:
                    interruption_source = next(
                        (
                            error
                            for error in cleanup_errors
                            if _interruption_in_chain(error) is not None
                        ),
                        None,
                    )
                if interruption_source is not None:
                    try:
                        raise ownership_error from startup_error
                    except _ProtocolStartupCleanupError as retained_error:
                        _raise_collected_errors(
                            (interruption_source, retained_error)
                        )
                raise ownership_error from startup_error
            _raise_collected_errors((startup_error,))

    @property
    def fatal(self) -> bool:
        with self._state_lock:
            return self._fatal_error is not None

    @property
    def pending_count(self) -> int:
        with self._state_lock:
            return len(self._pending)

    @property
    def pending_keys(self) -> tuple[tuple[str, int], ...]:
        with self._state_lock:
            return tuple(self._pending)

    def _sent_request_evidence(self) -> tuple[int, str | None]:
        """Return the monotonic dispatch sequence and most recent request method."""
        with self._state_lock:
            return self._sent_request_sequence, self._last_sent_request_method

    def expired_drain_keys(self, now: float) -> tuple[tuple[str, int], ...]:
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise TypeError("now must be a monotonic timestamp")
        if not math.isfinite(now):
            raise ValueError("now must be finite")
        with self._state_lock:
            return tuple(
                sorted(
                    key
                    for key, pending in self._pending.items()
                    if pending.drain_deadline is not None
                    and float(now) >= pending.drain_deadline
                )
            )

    def next_drain_deadline(self) -> float | None:
        with self._state_lock:
            return min(
                (
                    pending.drain_deadline
                    for pending in self._pending.values()
                    if pending.drain_deadline is not None
                ),
                default=None,
            )

    def request(
        self,
        method: str,
        params: object,
        *,
        deadline: float,
        cancellation: CancellationToken | None = None,
    ) -> object:
        if not isinstance(method, str) or not method:
            raise ValueError("method must be a non-empty string")
        if not isinstance(params, (dict, list)):
            raise TypeError("params must be an object or array")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            raise TypeError("deadline must be a monotonic timestamp")
        if not math.isfinite(deadline):
            raise ValueError("deadline must be finite")
        if cancellation is not None and not isinstance(cancellation, CancellationToken):
            raise TypeError("cancellation must be a CancellationToken")

        deadline = float(deadline)

        with self._state_lock:
            self._raise_if_unavailable_locked()
            cancelled_at = cancellation.cancelled_at if cancellation is not None else None
            now = time.monotonic()
            if cancelled_at is not None and cancelled_at <= deadline:
                raise RequestCancelled(f"LSP request cancelled: {method}")
            if now >= deadline:
                raise TimeoutError(f"LSP request timed out: {method}")
            if len(self._pending) >= MAX_PENDING_REQUESTS:
                raise PendingRequestLimitExceeded("at most 32 LSP requests may be active")
            request_id = self._allocate_request_id_locked()
            pending = PendingRequest(
                request_id,
                method,
                self.generation_nonce,
                deadline,
                threading.Event(),
                cancellation,
            )
            key = (self.generation_nonce, request_id)
            self._pending[key] = pending

        try:
            self._queue_request_message(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                deadline=deadline,
                key=key,
            )
        except BaseException:
            self._abandon_pending(key, pending)
            raise

        while True:
            now = time.monotonic()
            with self._state_lock:
                self._commit_due_locked(key, pending, now, source="requester")
                outcome = pending.terminal
                if outcome is not None:
                    break
            remaining = max(0.0, deadline - now)
            wait_for = remaining
            if cancellation is not None:
                wait_for = min(wait_for, _CANCELLATION_POLL_SECONDS)
            if pending.completed.wait(wait_for):
                time.sleep(0)

        if outcome == "cancelled":
            raise RequestCancelled(f"LSP request cancelled: {method}")
        if outcome == "timed_out":
            if pending.write_phase == "sending":
                error = TimeoutError("LSP write exceeded its deadline")
                self._become_fatal("LSP write exceeded its deadline", cause=error)
            raise TimeoutError(f"LSP request timed out: {method}")
        if outcome == "fatal":
            assert pending.terminal_error is not None
            raise pending.terminal_error
        if outcome == "closed":
            raise ProtocolViolation("LSP protocol is closed")
        if pending.error is not None:
            raise JsonRpcResponseError(pending.error)
        return pending.result

    def notify(self, method: str, params: object, *, deadline: float) -> None:
        """Deliver one notification through the single owned writer."""
        if not isinstance(method, str) or not method:
            raise ValueError("method must be a non-empty string")
        if not isinstance(params, (dict, list)):
            raise TypeError("params must be an object or array")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            raise TypeError("deadline must be a monotonic timestamp")
        if not math.isfinite(deadline):
            raise ValueError("deadline must be finite")
        self._write_message(
            {"jsonrpc": "2.0", "method": method, "params": params},
            deadline=float(deadline),
            wait=True,
        )

    def cancel_all(self, reason: str) -> None:
        """Cancel every active request without bypassing writer ownership."""
        if not isinstance(reason, str) or not reason or len(reason.encode("utf-8")) > 256:
            raise ValueError("reason must be a non-empty string of at most 256 bytes")
        completed: list[PendingRequest] = []
        now = time.monotonic()
        with self._state_lock:
            self._raise_if_unavailable_locked()
            for key, pending in tuple(self._pending.items()):
                if pending.terminal is None:
                    self._commit_local_locked(
                        key, pending, "cancelled", now, "manager"
                    )
                    completed.append(pending)
        for pending in completed:
            pending.completed.set()

    def close(self, deadline: float | None = None) -> None:
        if deadline is None:
            deadline = time.monotonic() + _OWNER_JOIN_SECONDS
        elif isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            raise TypeError("deadline must be a monotonic timestamp")
        elif not math.isfinite(deadline):
            raise ValueError("deadline must be finite")
        deadline = float(deadline)
        self._close_logically()
        self._io_stopped.set()
        self._cancel_owner_io(self.reader_thread)
        self._cancel_owner_io(self.writer_thread)
        self._interrupt_stream(self._reader)
        self._interrupt_stream(self._writer)
        try:
            self._write_queue.put_nowait(None)
        except queue.Full:
            pass
        self._join_owners(deadline)
        with self._state_lock:
            self._write_tasks.clear()
            self._ordinary_queued = 0
            self._control_queued = 0
        while True:
            try:
                self._write_queue.get_nowait()
            except queue.Empty:
                break

    def _close_logically(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            pending_items = tuple(self._pending.items())
            pending = tuple(request for _key, request in pending_items)
            closed_error = ProtocolViolation("LSP protocol is closed")
            closed_at = time.monotonic()
            for key, request in pending_items:
                self._commit_due_locked(key, request, closed_at, source="transport")
                if request.terminal is None:
                    request.terminal = "closed"
                    request.terminal_at = closed_at
                    request.terminal_source = "transport"
                    request.terminal_error = closed_error
            self._pending.clear()
            writes = tuple(self._write_tasks)
        for request in pending:
            request.completed.set()
        for task in writes:
            if task.error is None:
                task.error = closed_error
            task.completed.set()

    def _stop_io_for_process_cleanup(self) -> None:
        """Stop owned I/O without synchronously closing process pipes."""
        self._close_logically()
        self._io_stopped.set()
        try:
            self._write_queue.put_nowait(None)
        except queue.Full:
            pass
        for owner in (self.reader_thread, self.writer_thread):
            if owner.ident is not None:
                self._cancel_owner_io(owner)

    def _finish_io_after_process_exit(self, deadline: float) -> None:
        """Close process pipes and owners only after the child is confirmed dead."""
        self.close(deadline)

    def _allocate_request_id_locked(self) -> int:
        if self._next_request_id > _JSON_RPC_INTEGER_MAX:
            raise _LocalRequestViolation("JSON-RPC request ID space exhausted")
        request_id = self._next_request_id
        self._next_request_id += 1
        return request_id

    def _raise_if_unavailable_locked(self) -> None:
        if self._fatal_error is not None:
            raise self._fatal_error
        if self._closed:
            raise ProtocolViolation("LSP protocol is closed")

    def _reader_loop(self) -> None:
        if not self._register_owner("reader", self._reader_started):
            return
        self.stdout_reader_owner = threading.get_ident()
        frame_reader = JsonRpcFrameReader(_OwnedReader(self._reader, self._io_stopped))
        try:
            while True:
                with self._state_lock:
                    if self._closed or self._fatal_error is not None:
                        return
                message = frame_reader.read()
                self._dispatch_message(message, generation_nonce=self.generation_nonce)
                with self._state_lock:
                    if self._closed or self._fatal_error is not None:
                        return
        except ProtocolViolation as exc:
            with self._state_lock:
                closed = self._closed
            if not closed:
                self._become_fatal(str(exc), cause=exc)
        except (OSError, ValueError) as exc:
            with self._state_lock:
                closed = self._closed
            if not closed:
                self._become_fatal("failed to read LSP stdout", cause=exc)
        finally:
            self._close_stream(self._reader)
            self._release_owner("reader")

    def _writer_loop(self) -> None:
        if not self._register_owner("writer", self._writer_started):
            return
        self.stdin_writer_owner = threading.get_ident()
        try:
            while True:
                task = self._write_queue.get()
                if task is None:
                    return
                request_method: str | None = None
                with self._state_lock:
                    if task.control:
                        self._control_queued -= 1
                    else:
                        self._ordinary_queued -= 1
                    stopped = self._closed or self._fatal_error is not None
                    pending = (
                        self._pending.get(task.request_key)
                        if task.request_key is not None
                        else None
                    )
                    if pending is not None and pending.write_phase == "queued":
                        self._commit_due_locked(
                            task.request_key,
                            pending,
                            time.monotonic(),
                            source="writer",
                        )
                    skip_request = task.request_key is not None and (
                        task.request_key in self._cancelled_keys
                        or pending is None
                        or pending.terminal is not None
                    )
                    if pending is not None and not skip_request:
                        pending.write_phase = "sending"
                        request_method = pending.method
                if stopped:
                    self._complete_write(task, ProtocolViolation("LSP protocol stopped"))
                    return
                if skip_request:
                    self._complete_write(task, None)
                    continue
                if time.monotonic() >= task.deadline:
                    if task.best_effort:
                        self._complete_write(task, None)
                        continue
                    error = TimeoutError("LSP write deadline expired")
                    self._become_fatal("LSP write deadline expired", cause=error)
                    self._complete_write(task, error)
                    return
                try:
                    self._write_frame(task)
                except BaseException as exc:
                    with self._state_lock:
                        closed = self._closed
                    if not closed:
                        self._become_fatal("failed to write LSP message", cause=exc)
                    self._complete_write(task, exc)
                    return
                if request_method is not None:
                    with self._state_lock:
                        self._sent_request_sequence += 1
                        self._last_sent_request_method = request_method
                if time.monotonic() > task.deadline:
                    if task.best_effort:
                        self._complete_write(task, None)
                        continue
                    error = TimeoutError("LSP write exceeded its deadline")
                    self._become_fatal("LSP write exceeded its deadline", cause=error)
                    self._complete_write(task, error)
                    return
                with self._state_lock:
                    pending = (
                        self._pending.get(task.request_key)
                        if task.request_key is not None
                        else None
                    )
                    if pending is not None:
                        pending.write_phase = "sent"
                        if pending.terminal in {"cancelled", "timed_out"}:
                            self._enqueue_cancel_locked(pending)
                self._complete_write(task, None)
        finally:
            self._close_stream(self._writer)
            self._release_owner("writer")

    def _dispatch_message(self, message: dict[str, Any], *, generation_nonce: str) -> None:
        if generation_nonce != self.generation_nonce:
            return
        if "method" in message:
            if "id" in message:
                self._handle_server_request(message)
            else:
                self._handle_server_notification(message)
            return
        self._handle_response(message, generation_nonce)

    def _handle_response(self, message: dict[str, Any], generation_nonce: str) -> None:
        response_id = message["id"]
        if isinstance(response_id, str):
            self._become_fatal("server response ID does not match an active request")
            return
        key = (generation_nonce, response_id)
        violation: ProtocolViolation | None = None
        with self._state_lock:
            if key in self._responded_keys:
                violation = ProtocolViolation("duplicate active response ID")
            else:
                pending = self._pending.get(key)
                if pending is None:
                    return
                try:
                    if "result" in message:
                        self._validate_result(pending.method, message["result"])
                        pending.result = message["result"]
                    else:
                        raw_error = message["error"]
                        pending.error = JsonRpcError(
                            raw_error["code"], raw_error["message"], raw_error.get("data")
                        )
                except ProtocolViolation as exc:
                    violation = exc
                if violation is None:
                    responded_at = time.monotonic()
                    pending.responded_at = responded_at
                    if pending.terminal is None:
                        self._commit_response_locked(key, pending, responded_at)
                    elif pending.terminal in {"cancelled", "timed_out"}:
                        self._pending.pop(key, None)
                        self._remember_key(key, self._cancelled_keys, self._cancelled_order)
                    pending.completed.set()
        if violation is not None:
            self._become_fatal(str(violation), cause=violation)

    def _validate_result(self, method: str, result: object) -> None:
        if method in _FLAT_SEMANTIC_RESULT_METHODS:
            if isinstance(result, list) and len(result) > MAX_LOCATIONS:
                raise ProtocolViolation("location result exceeds 10,000 items")
        elif method == "textDocument/documentSymbol":
            self._validate_document_symbol_count(result)
        if method == "textDocument/hover":
            try:
                size = len(
                    json.dumps(
                        result,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            except (TypeError, ValueError, RecursionError) as exc:
                raise ProtocolViolation("hover result is not strict JSON") from exc
            if size > MAX_HOVER_BYTES:
                raise ProtocolViolation("hover result exceeds 256 KiB")

    @staticmethod
    def _validate_document_symbol_count(result: object) -> None:
        if not isinstance(result, list):
            return
        if len(result) > MAX_LOCATIONS:
            raise ProtocolViolation("location result exceeds 10,000 items")
        count = 0
        seen: set[int] = set()
        stack: list[object] = list(result)
        while stack:
            symbol = stack.pop()
            count += 1
            if count > MAX_LOCATIONS:
                raise ProtocolViolation("location result exceeds 10,000 items")
            if not isinstance(symbol, dict):
                continue
            identity = id(symbol)
            if identity in seen:
                raise ProtocolViolation("document symbol result contains a cycle")
            seen.add(identity)
            children = symbol.get("children")
            if isinstance(children, list):
                if count + len(stack) + len(children) > MAX_LOCATIONS:
                    raise ProtocolViolation("location result exceeds 10,000 items")
                stack.extend(children)

    def _handle_server_request(self, message: dict[str, Any]) -> None:
        method = message["method"]
        request_id = message["id"]
        if method not in SERVER_REQUESTS:
            self._write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": METHOD_NOT_FOUND, "message": "Method not found"},
                }
            )
            return
        handler = self._server_request_handlers.get(method)
        try:
            if handler is not None:
                result = handler(message.get("params"))
            elif method == "workspace/configuration":
                result = []
            else:
                result = None
            self._write_message({"jsonrpc": "2.0", "id": request_id, "result": result})
        except BaseException:
            self._write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32603, "message": "Internal error"},
                }
            )

    def _handle_server_notification(self, message: dict[str, Any]) -> None:
        method = message["method"]
        if method not in SERVER_NOTIFICATIONS:
            callback: Callable[[str], None] | None = None
            with self._state_lock:
                if not self._unknown_notification_warned:
                    self._unknown_notification_warned = True
                    callback = self._warning_callback
            if callback is not None:
                try:
                    callback(_UNKNOWN_NOTIFICATION_WARNING)
                except BaseException:
                    pass
            return

        params = message.get("params")
        if method == "textDocument/publishDiagnostics":
            if not isinstance(params, dict) or not isinstance(params.get("diagnostics"), list):
                self._become_fatal("diagnostic notification has invalid shape")
                return
            if len(params["diagnostics"]) > MAX_DIAGNOSTICS:
                self._become_fatal("diagnostic notification exceeds 10,000 items")
                return
        handler = self._server_notification_handlers.get(method)
        if handler is not None:
            try:
                handler(params)
            except BaseException:
                pass

    def _commit_due_locked(
        self,
        key: tuple[str, int],
        pending: PendingRequest,
        now: float,
        *,
        source: str,
    ) -> None:
        if pending.terminal is not None:
            return

        cancelled_at = (
            pending.cancellation.cancelled_at
            if pending.cancellation is not None
            else None
        )
        if (
            cancelled_at is not None
            and cancelled_at <= pending.deadline
            and cancelled_at <= now
        ):
            self._commit_local_locked(key, pending, "cancelled", cancelled_at, source)
        elif pending.deadline <= now:
            self._commit_local_locked(
                key, pending, "timed_out", pending.deadline, source
            )

    def _commit_response_locked(
        self,
        key: tuple[str, int],
        pending: PendingRequest,
        responded_at: float,
    ) -> None:
        cancelled_at = (
            pending.cancellation.cancelled_at
            if pending.cancellation is not None
            else None
        )
        if (
            cancelled_at is not None
            and cancelled_at <= pending.deadline
            and cancelled_at <= responded_at
        ):
            self._commit_local_locked(
                key, pending, "cancelled", cancelled_at, "reader"
            )
            self._pending.pop(key, None)
            self._remember_key(key, self._cancelled_keys, self._cancelled_order)
            return
        if pending.deadline <= responded_at:
            self._commit_local_locked(
                key, pending, "timed_out", pending.deadline, "reader"
            )
            self._pending.pop(key, None)
            self._remember_key(key, self._cancelled_keys, self._cancelled_order)
            return
        pending.terminal = "response"
        pending.terminal_at = responded_at
        pending.terminal_source = "reader"
        self._pending.pop(key, None)
        self._remember_key(key, self._responded_keys, self._responded_order)

    def _commit_local_locked(
        self,
        key: tuple[str, int],
        pending: PendingRequest,
        outcome: str,
        terminal_at: float,
        source: str,
    ) -> None:
        if pending.terminal is not None:
            return
        pending.terminal = outcome
        pending.terminal_at = terminal_at
        pending.terminal_source = source
        if pending.write_phase == "queued":
            self._pending.pop(key, None)
            self._remember_key(key, self._cancelled_keys, self._cancelled_order)
        else:
            pending.drain_deadline = terminal_at + CANCEL_DRAIN_GRACE_SECONDS
            if self._drain_wake is not None:
                self._drain_wake.set()
            if pending.write_phase == "sent":
                self._enqueue_cancel_locked(pending)
        pending.completed.set()

    def _enqueue_cancel_locked(self, pending: PendingRequest) -> None:
        if pending.cancel_enqueued:
            return
        pending.cancel_enqueued = True
        frame = encode_frame(
            {
                "jsonrpc": "2.0",
                "method": "$/cancelRequest",
                "params": {"id": pending.request_id},
            }
        )
        task = _WriteTask(
            frame,
            max(pending.deadline, time.monotonic() + _INTERNAL_WRITE_SECONDS),
            threading.Event(),
            best_effort=True,
            control=True,
        )
        if not self._enqueue_write_locked(task):
            raise ProtocolViolation("LSP cancellation control queue invariant breached")

    def _abandon_pending(self, key: tuple[str, int], pending: PendingRequest) -> None:
        with self._state_lock:
            if self._pending.get(key) is pending:
                self._pending.pop(key, None)
                self._remember_key(key, self._cancelled_keys, self._cancelled_order)

    @staticmethod
    def _remember_key(
        key: tuple[str, int],
        values: set[tuple[str, int]],
        order: deque[tuple[str, int]],
    ) -> None:
        if key in values:
            return
        values.add(key)
        order.append(key)
        if len(order) > _TOMBSTONE_LIMIT:
            values.discard(order.popleft())

    def _write_message(
        self,
        message: object,
        *,
        deadline: float | None = None,
        wait: bool = False,
    ) -> None:
        if deadline is None:
            deadline = time.monotonic() + _INTERNAL_WRITE_SECONDS
        frame = encode_frame(message)
        if time.monotonic() >= deadline:
            error = TimeoutError("LSP write deadline expired")
            self._become_fatal("LSP write deadline expired", cause=error)
            raise error
        task = _WriteTask(frame, deadline, threading.Event())
        queue_full = False
        with self._state_lock:
            self._raise_if_unavailable_locked()
            queue_full = not self._enqueue_write_locked(task)
        if queue_full:
            exc = queue.Full()
            self._complete_write(task, exc)
            self._become_fatal("LSP write queue deadline expired", cause=exc)
            raise TimeoutError("LSP write queue deadline expired") from exc
        if not wait:
            return
        remaining = max(0.0, deadline - time.monotonic())
        if not task.completed.wait(remaining):
            error = TimeoutError("LSP write exceeded its deadline")
            self._become_fatal("LSP write exceeded its deadline", cause=error)
            raise error
        if task.error is not None:
            with self._state_lock:
                fatal_error = self._fatal_error
                closed = self._closed
            if fatal_error is not None:
                raise fatal_error
            if closed:
                raise ProtocolViolation("LSP protocol is closed")
            raise ProtocolViolation("failed to write LSP message") from task.error

    def _queue_request_message(
        self,
        message: object,
        *,
        deadline: float,
        key: tuple[str, int],
    ) -> None:
        try:
            frame = encode_frame(message)
        except ProtocolViolation as error:
            raise _LocalRequestViolation(str(error)) from error
        task = _WriteTask(frame, deadline, threading.Event(), request_key=key)
        queue_full = False
        with self._state_lock:
            self._raise_if_unavailable_locked()
            queue_full = not self._enqueue_write_locked(task)
        if queue_full:
            exc = queue.Full()
            self._complete_write(task, exc)
            self._become_fatal("LSP write queue deadline expired", cause=exc)
            raise TimeoutError("LSP write queue deadline expired") from exc

    def _enqueue_write_locked(self, task: _WriteTask) -> bool:
        if task.control:
            if self._ordinary_queued + self._control_queued >= _MAX_QUEUED_WRITES:
                return False
        elif self._ordinary_queued >= _MAX_ORDINARY_WRITES:
            return False
        self._write_tasks.append(task)
        try:
            self._write_queue.put_nowait(task)
        except queue.Full as exc:  # pragma: no cover - counters and queue share the lock
            self._write_tasks.remove(task)
            raise ProtocolViolation("LSP write queue accounting invariant breached") from exc
        if task.control:
            self._control_queued += 1
        else:
            self._ordinary_queued += 1
        return True

    def _complete_write(self, task: _WriteTask, error: BaseException | None) -> None:
        with self._state_lock:
            if task in self._write_tasks:
                self._write_tasks.remove(task)
            if task.error is None:
                task.error = error
        task.completed.set()

    def _write_frame(self, task: _WriteTask) -> None:
        if os.name == "nt":
            self._writer.write(task.frame)
            self._writer.flush()
            return
        try:
            descriptor = self._writer.fileno()
        except (AttributeError, OSError):
            self._writer.write(task.frame)
            self._writer.flush()
            return
        was_blocking = os.get_blocking(descriptor)
        os.set_blocking(descriptor, False)
        offset = 0
        frame = memoryview(task.frame)
        try:
            while offset < len(task.frame):
                if self._io_stopped.is_set() or time.monotonic() >= task.deadline:
                    raise TimeoutError("LSP write exceeded its deadline")
                remaining = min(0.05, max(0.0, task.deadline - time.monotonic()))
                _, writable, _ = select.select([], [descriptor], [], remaining)
                if not writable:
                    continue
                try:
                    written = os.write(descriptor, frame[offset:])
                except BlockingIOError:
                    continue
                if written <= 0:
                    raise OSError("LSP writer made no progress")
                offset += written
        finally:
            try:
                os.set_blocking(descriptor, was_blocking)
            except OSError:
                pass

    def _become_fatal(self, reason: str, *, cause: BaseException | None = None) -> None:
        callback: Callable[[str], None] | None = None
        with self._state_lock:
            if self._fatal_error is not None or self._closed:
                return
            if isinstance(cause, ProtocolViolation):
                error = cause
            else:
                error = ProtocolViolation(reason)
                if cause is not None:
                    error.__cause__ = cause
            self._fatal_error = error
            pending_items = tuple(self._pending.items())
            pending = tuple(request for _key, request in pending_items)
            fatal_at = time.monotonic()
            for key, request in pending_items:
                self._commit_due_locked(key, request, fatal_at, source="transport")
                if request.terminal is None:
                    request.terminal = "fatal"
                    request.terminal_at = fatal_at
                    request.terminal_source = "transport"
                    request.terminal_error = error
            self._pending.clear()
            writes = tuple(self._write_tasks)
            callback = self._fatal_callback
        for request in pending:
            request.completed.set()
        for task in writes:
            if task.error is None:
                task.error = error
            task.completed.set()
        self._io_stopped.set()
        self._cancel_owner_io(self.reader_thread)
        self._cancel_owner_io(self.writer_thread)
        self._interrupt_stream(self._reader)
        self._interrupt_stream(self._writer)
        try:
            self._write_queue.put_nowait(None)
        except queue.Full:
            pass
        try:
            callback(reason)
        except BaseException:
            pass

    @staticmethod
    def _stream_layers(stream: BinaryIO) -> tuple[object, ...]:
        layers: list[object] = []
        identities: set[int] = set()
        current: object | None = stream
        while current is not None and id(current) not in identities:
            layers.append(current)
            identities.add(id(current))
            next_layer = getattr(current, "raw", None)
            if next_layer is None:
                next_layer = getattr(current, "buffer", None)
            current = next_layer
        return tuple(layers)

    @classmethod
    def _interrupt_stream(cls, stream: BinaryIO) -> None:
        layers = cls._stream_layers(stream)
        for layer in reversed(layers):
            sock = getattr(layer, "_sock", None)
            if sock is not None:
                try:
                    sock.shutdown(2)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
            close = getattr(layer, "close", None)
            if close is not None:
                try:
                    close()
                except (OSError, ValueError):
                    pass
                break

    @staticmethod
    def _close_stream(stream: BinaryIO) -> None:
        try:
            stream.close()
        except (OSError, ValueError):
            pass

    def _join_owner(self, owner: threading.Thread, deadline: float) -> None:
        if owner is threading.current_thread():
            return
        if owner.ident is None and owner not in threading.enumerate():
            name = "reader" if owner is self.reader_thread else "writer"
            self._release_owner(name)
            with self._owner_handle_lock:
                release_error = self._owner_release_errors.get(name)
            if release_error is not None:
                raise release_error
            return
        remaining = deadline - time.monotonic()
        if remaining > 0:
            owner.join(remaining)
        if owner.is_alive():
            error = TimeoutError("LSP protocol owner did not stop before deadline")
            name = "reader" if owner is self.reader_thread else "writer"
            with self._owner_handle_lock:
                interruption = self._owner_interrupt_errors.get(name)
            if interruption is not None:
                error.__cause__ = interruption
            raise error
        name = "reader" if owner is self.reader_thread else "writer"
        self._release_owner(name)
        with self._owner_handle_lock:
            self._owner_interrupt_errors.pop(name, None)
            release_error = self._owner_release_errors.get(name)
        if release_error is not None:
            raise release_error

    def _join_owners(self, deadline: float) -> None:
        errors: list[BaseException] = []
        for owner in (self.reader_thread, self.writer_thread):
            try:
                self._join_owner(owner, deadline)
            except BaseException as exc:
                errors.append(exc)
        _raise_collected_errors(errors)

    @staticmethod
    def _require_owner_start_budget(deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("LSP owner thread-start deadline expired")

    @staticmethod
    def _wait_owner_started(
        event: threading.Event, owner_name: str, deadline: float
    ) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not event.wait(remaining):
            raise TimeoutError(f"LSP {owner_name} owner did not start before deadline")

    def _wait_owner_registration(
        self,
        event: threading.Event,
        owner_name: str,
        deadline: float,
    ) -> None:
        try:
            self._wait_owner_started(event, owner_name, deadline)
        except TimeoutError:
            with self._owner_handle_lock:
                registered_at = self._owner_registration_monotonic.get(owner_name)
            if registered_at is None or registered_at > deadline:
                raise

    def _join_partially_started_owner(
        self,
        owner: threading.Thread,
        deadline: float,
    ) -> None:
        while owner.ident is not None or owner in threading.enumerate():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if owner.is_alive():
                    raise TimeoutError(
                        "LSP protocol owner did not stop during startup cleanup"
                    )
                break
            try:
                owner.join(remaining)
            except RuntimeError:
                if time.monotonic() >= deadline:
                    if owner.is_alive():
                        raise TimeoutError(
                            "LSP protocol owner did not stop during startup cleanup"
                        )
                    break
                time.sleep(min(0.001, max(0.0, deadline - time.monotonic())))
                continue
            if owner.is_alive():
                raise TimeoutError(
                    "LSP protocol owner did not stop during startup cleanup"
                )
            break
        name = "reader" if owner is self.reader_thread else "writer"
        self._release_owner(name)
        with self._owner_handle_lock:
            release_error = self._owner_release_errors.get(name)
        if release_error is not None:
            raise release_error

    def _register_owner(self, name: str, started: threading.Event) -> bool:
        if os.name != "nt":
            with self._owner_handle_lock:
                self._owner_registration_monotonic[name] = time.monotonic()
            started.set()
            return True
        try:
            handle = _duplicate_current_thread_handle()
        except BaseException as error:
            with self._owner_handle_lock:
                self._owner_start_errors[name] = error
                self._owner_registration_monotonic[name] = time.monotonic()
            started.set()
            return False
        with self._owner_handle_lock:
            if name == "reader":
                self._reader_os_handle = handle
            else:
                self._writer_os_handle = handle
            self._owner_registration_monotonic[name] = time.monotonic()
        started.set()
        return True

    def _raise_owner_start_error(self) -> None:
        with self._owner_handle_lock:
            errors = tuple(self._owner_start_errors.values())
        _raise_collected_errors(errors)

    def _release_owner(self, name: str) -> None:
        if os.name != "nt":
            return
        with self._owner_handle_lock:
            handle = (
                self._reader_os_handle
                if name == "reader"
                else self._writer_os_handle
            )
            if handle is None:
                return
            if not _KERNEL32.CloseHandle(handle):
                self._owner_release_errors[name] = ctypes.WinError(
                    ctypes.get_last_error()
                )
                return
            self._owner_release_errors.pop(name, None)
            if name == "reader":
                self._reader_os_handle = None
            else:
                self._writer_os_handle = None

    def _cancel_owner_io(self, owner: threading.Thread) -> None:
        if os.name != "nt" or owner is threading.current_thread():
            return
        name = "reader" if owner is self.reader_thread else "writer"
        with self._owner_handle_lock:
            handle = (
                self._reader_os_handle
                if name == "reader"
                else self._writer_os_handle
            )
            if handle is None:
                return
            if _KERNEL32.CancelSynchronousIo(handle):
                self._owner_interrupt_errors.pop(name, None)
                return
            error_number = ctypes.get_last_error()
            if error_number == _ERROR_NOT_FOUND:
                self._owner_interrupt_errors.pop(name, None)
                return
            self._owner_interrupt_errors[name] = ctypes.WinError(error_number)


if os.name == "nt":
    def _duplicate_current_thread_handle() -> int:
        process = _KERNEL32.GetCurrentProcess()
        duplicated = wintypes.HANDLE()
        if not _KERNEL32.DuplicateHandle(
            process,
            _KERNEL32.GetCurrentThread(),
            process,
            ctypes.byref(duplicated),
            0,
            False,
            _DUPLICATE_SAME_ACCESS,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(duplicated.value)
