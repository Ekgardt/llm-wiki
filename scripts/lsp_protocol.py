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

from interruption import (
    exception_reaches as _exception_reaches,
)
from interruption import (
    interruption_in_chain as _interruption_in_chain,
)

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


def _descriptor_of(stream: BinaryIO) -> int | None:
    try:
        return stream.fileno()
    except (AttributeError, OSError):
        return None


def _readable_within(descriptor: int, seconds: float) -> bool | None:
    """Whether the descriptor is readable; None when the wait itself failed."""
    try:
        readable, _, _ = select.select([descriptor], [], [], seconds)
    except (OSError, ValueError):
        return None
    return bool(readable)


def _read_or_empty(descriptor: int, size: int) -> bytes:
    try:
        return os.read(descriptor, size)
    except OSError:
        return b""


class _OwnedReader:
    def __init__(self, stream: BinaryIO, stopped: threading.Event) -> None:
        self._stream = stream
        self._stopped = stopped

    def read(self, size: int = -1) -> bytes:
        if os.name == "nt":
            return self._stream.read(size)
        descriptor = _descriptor_of(self._stream)
        if descriptor is None:
            return self._stream.read(size)
        return self._read_descriptor(descriptor, size)

    def _read_descriptor(self, descriptor: int, size: int) -> bytes:
        while not self._stopped.is_set():
            readable = _readable_within(descriptor, 0.05)
            if readable is None:
                return b""
            if readable:
                return _read_or_empty(descriptor, size)
        return b""


def _strict_string_size(value: str) -> int:
    try:
        return len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ProtocolViolation("JSON strings must not contain lone surrogates") from exc


class _JsonWalk:
    """The counters one strict-JSON walk keeps: values, string bytes, open containers."""

    def __init__(self) -> None:
        self.maximum = 0
        self.values_seen = 0
        self.string_bytes = 0
        self.active_containers: set[int] = set()
        self.stack: list[tuple[object, int, bool]] = []

    def count_value(self) -> None:
        self.values_seen += 1
        if self.values_seen > _MAX_JSON_VALUES:
            raise ProtocolViolation("JSON value count exceeds the frame bound")

    def count_string(self, text: str) -> None:
        self.string_bytes += _strict_string_size(text)
        if self.string_bytes > MAX_FRAME_BYTES:
            raise ProtocolViolation("JSON strings exceed the frame bound")

    def enter_container(self, current: object, parent_depth: int) -> int:
        identity = id(current)
        if identity in self.active_containers:
            raise ProtocolViolation("cyclic JSON values are not supported")
        self.active_containers.add(identity)
        depth = parent_depth + 1
        self.maximum = max(self.maximum, depth)
        self.stack.append((current, depth, True))
        return depth

    def push_children(self, current: object, depth: int) -> None:
        if isinstance(current, dict):
            self._push_object(current, depth)
            return
        for child in reversed(current):
            self.stack.append((child, depth, False))

    def _push_object(self, current: dict, depth: int) -> None:
        for key, child in current.items():
            _require_json_key(key)
            self.count_string(key)
            self.stack.append((child, depth, False))


def _require_finite_number(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ProtocolViolation("JSON numbers must be finite")


def _require_json_key(key: object) -> None:
    _require_finite_number(key)
    if not isinstance(key, str):
        raise ProtocolViolation("JSON object keys must be strings")


def _scalar_is_done(walk: _JsonWalk, current: object) -> bool:
    """True when `current` is a strict scalar (and counted); False for a container."""
    if isinstance(current, str):
        walk.count_string(current)
        return True
    _require_finite_number(current)
    return _non_string_scalar(current)


def _non_string_scalar(current: object) -> bool:
    if current is None or isinstance(current, (bool, int, float)):
        return True
    if not isinstance(current, (dict, list)):
        raise ProtocolViolation("value is not a strict JSON type")
    return False


def json_depth(value: object) -> int:
    """Validate a strict JSON value and return its bounded container depth."""
    walk = _JsonWalk()
    walk.stack.append((value, 0, False))
    while walk.stack:
        current, parent_depth, exiting = walk.stack.pop()
        if exiting:
            walk.active_containers.remove(id(current))
            continue
        walk.count_value()
        if _scalar_is_done(walk, current):
            continue
        walk.push_children(current, walk.enter_container(current, parent_depth))
    return walk.maximum


def _valid_id(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return _JSON_RPC_INTEGER_MIN <= value <= _JSON_RPC_INTEGER_MAX
    return isinstance(value, str)


def _error_shape_is_valid(value: dict) -> bool:
    return set(value) <= {"code", "message", "data"} and {"code", "message"} <= set(value)


def _valid_error_code(code: object) -> bool:
    if isinstance(code, bool) or not isinstance(code, int):
        return False
    return _JSON_RPC_INTEGER_MIN <= code <= _JSON_RPC_INTEGER_MAX


def _validate_error(value: object) -> None:
    if not isinstance(value, dict):
        raise ProtocolViolation("response error must be an object")
    if not _error_shape_is_valid(value):
        raise ProtocolViolation("response error has invalid shape")
    _validate_error_fields(value)


def _validate_error_fields(value: dict) -> None:
    if not _valid_error_code(value["code"]):
        raise ProtocolViolation("response error code is invalid")
    if not isinstance(value["message"], str):
        raise ProtocolViolation("response error message is invalid")


def _validate_request_id_and_params(value: dict) -> None:
    if "id" in value and not _valid_id(value["id"]):
        raise ProtocolViolation("request ID is invalid")
    if "params" in value and not isinstance(value["params"], (dict, list)):
        raise ProtocolViolation("params must be an object or array")


def _validate_request(value: dict) -> dict[str, Any]:
    if not set(value) <= {"jsonrpc", "id", "method", "params"}:
        raise ProtocolViolation("request or notification has invalid shape")
    if not isinstance(value["method"], str) or not value["method"]:
        raise ProtocolViolation("method must be a non-empty string")
    _validate_request_id_and_params(value)
    return value


def _validate_response_payload(value: dict) -> None:
    if ("result" in value) == ("error" in value):
        raise ProtocolViolation("response must contain exactly one of result or error")
    if "error" in value:
        _validate_error(value["error"])


def _validate_response(value: dict) -> dict[str, Any]:
    if not set(value) <= {"jsonrpc", "id", "result", "error"}:
        raise ProtocolViolation("response has invalid shape")
    if "id" not in value or not _valid_id(value["id"]):
        raise ProtocolViolation("response ID is invalid")
    _validate_response_payload(value)
    return value


def _validate_message(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolViolation("JSON-RPC batches and scalar messages are not supported")
    if value.get("jsonrpc") != "2.0":
        raise ProtocolViolation("JSON-RPC version must be 2.0")
    return _validate_by_role(value)


def _validate_by_role(value: dict[str, Any]) -> dict[str, Any]:
    if "method" in value:
        return _validate_request(value)
    return _validate_response(value)


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


def _read_header_bytes(stream: BinaryIO) -> bytearray:
    header = bytearray()
    while not header.endswith(b"\r\n\r\n"):
        octet = stream.read(1)
        if not octet:
            raise ProtocolViolation("unexpected EOF in LSP header")
        header.extend(octet)
        if len(header) > MAX_HEADER_BYTES:
            raise ProtocolViolation("LSP header exceeds 8 KiB")
    return header


def _header_lines(header: bytearray) -> list[str]:
    try:
        return bytes(header[:-4]).decode("ascii", errors="strict").split("\r\n")
    except UnicodeDecodeError as exc:
        raise ProtocolViolation("LSP header must be ASCII") from exc


def _require_header_field(name: str, field_value: str, lowered: str, headers: dict) -> None:
    if not name or not field_value or lowered in headers:
        raise ProtocolViolation("LSP header field is missing or duplicated")


def _header_fields(lines: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in lines:
        if ": " not in line:
            raise ProtocolViolation("LSP header field is malformed")
        name, field_value = line.split(": ", 1)
        lowered = name.lower()
        _require_header_field(name, field_value, lowered, headers)
        headers[lowered] = field_value
    return headers


def _content_length_text_is_valid(length_text: str | None) -> bool:
    if length_text is None:
        return False
    return length_text.isascii() and length_text.isdecimal()


def _require_length_text(length_text: str | None) -> None:
    if not _content_length_text_is_valid(length_text):
        raise ProtocolViolation("Content-Length is missing or invalid")
    if len(length_text) > len(str(MAX_FRAME_BYTES)):
        raise ProtocolViolation("Content-Length exceeds the frame limit")


def _content_length(headers: dict[str, str]) -> int:
    length_text = headers.get("content-length")
    _require_length_text(length_text)
    length = int(length_text)
    if length > MAX_FRAME_BYTES:
        raise ProtocolViolation("LSP frame exceeds 8 MiB")
    return length


def _content_type_parts(content_type: str) -> tuple[str, list[str]]:
    parts = [part.strip() for part in content_type.lower().split(";")]
    return parts[0], [part for part in parts[1:] if part]


def _require_content_type(headers: dict[str, str]) -> None:
    content_type = headers.get("content-type")
    if content_type is None:
        return
    _require_media_type(*_content_type_parts(content_type))


def _require_media_type(media_type: str, parameters: list[str]) -> None:
    if media_type != "application/vscode-jsonrpc":
        raise ProtocolViolation("unsupported LSP content type")
    if parameters not in ([], ["charset=utf-8"], ["charset=utf8"]):
        raise ProtocolViolation("unsupported LSP charset")


def _read_body(stream: BinaryIO, length: int) -> bytes:
    body = bytearray()
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            raise ProtocolViolation("unexpected EOF in LSP body")
        body.extend(chunk)
    return bytes(body)


class JsonRpcFrameReader:
    """Read strict Content-Length framed JSON-RPC messages from one binary stream."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream

    def read(self) -> dict[str, Any]:
        headers = _header_fields(_header_lines(_read_header_bytes(self._stream)))
        length = _content_length(headers)
        _require_content_type(headers)
        return _decode_body(_read_body(self._stream, length))


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






def _interruption_source(
    errors: Sequence[BaseException],
) -> tuple[BaseException | None, KeyboardInterrupt | SystemExit | None]:
    """(the error carrying an interruption, the interruption), or (None, None)."""
    for error in errors:
        interruption = _interruption_in_chain(error)
        if interruption is not None:
            return error, interruption
    return None, None


def _other_error(errors, source, interruption) -> BaseException | None:
    for error in errors:
        if error is not source and error is not interruption:
            return error
    return None


def _secondary_error(errors, source, interruption) -> BaseException | None:
    secondary = _other_error(errors, source, interruption)
    if secondary is None and source is not interruption:
        return source
    return secondary


def _scrub_cause(current: BaseException, interruption, pending: list) -> None:
    cause = current.__cause__
    if cause is interruption:
        current.__cause__ = None
        return
    if cause is not None:
        pending.append(cause)


def _scrub_context(current: BaseException, interruption, pending: list) -> None:
    context = current.__context__
    if context is interruption or context is current.__cause__:
        current.__context__ = None
        return
    if context is not None:
        pending.append(context)


def _scrub_interruption_links(secondary: BaseException, interruption) -> None:
    """Cut every link from the secondary error's chain back to the interruption."""
    pending = [secondary]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        _scrub_cause(current, interruption, pending)
        _scrub_context(current, interruption, pending)


def _scrubbed_secondary(secondary: BaseException | None, interruption) -> BaseException | None:
    if secondary is None:
        return None
    _scrub_interruption_links(secondary, interruption)
    if _exception_reaches(secondary, interruption):
        return None
    return secondary


def _drop_self_cause(interruption: BaseException) -> None:
    if _exception_reaches(interruption.__cause__, interruption):
        interruption.__cause__ = None


def _response_value(pending: PendingRequest) -> object:
    if pending.error is not None:
        raise JsonRpcResponseError(pending.error)
    return pending.result


def _require_not_cancelled_or_late(
    method: str, deadline: float, cancelled_at: float | None, now: float
) -> None:
    if cancelled_at is not None and cancelled_at <= deadline:
        raise RequestCancelled(f"LSP request cancelled: {method}")
    if now >= deadline:
        raise TimeoutError(f"LSP request timed out: {method}")


def _detach_self_links(interruption: BaseException) -> None:
    _drop_self_cause(interruption)
    if _exception_reaches(interruption.__context__, interruption):
        interruption.__context__ = None
    if interruption.__cause__ is not None:
        interruption.__context__ = None


def _raise_interruption(interruption: BaseException, secondary: BaseException | None) -> None:
    try:
        if secondary is not None:
            raise interruption.with_traceback(interruption.__traceback__) from secondary
        raise interruption.with_traceback(interruption.__traceback__)
    except (KeyboardInterrupt, SystemExit) as raised:
        if raised is not interruption:
            raise
        _detach_self_links(interruption)
        raise


def _raise_collected_errors(errors: Sequence[BaseException]) -> None:
    """Raise the interruption if one is travelling, otherwise the first error.

    Unlike the shared helper this scrubs the whole chain of the secondary
    error, not only its immediate cause and context, because a protocol
    rollback can nest the interruption several links down.
    """
    if not errors:
        return
    source, interruption = _interruption_source(errors)
    if interruption is None:
        raise errors[0]
    secondary = _scrubbed_secondary(_secondary_error(errors, source, interruption), interruption)
    _raise_interruption(interruption, secondary)


def _require_monotonic(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a monotonic timestamp")
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return float(value)


def _require_drain_wake(_drain_wake: object) -> None:
    if _drain_wake is not None and not isinstance(_drain_wake, threading.Event):
        raise TypeError("_drain_wake must be a threading.Event or None")


def _require_protocol_arguments(generation_nonce: object, fatal_callback: object, _drain_wake: object) -> None:
    if not isinstance(generation_nonce, str) or not generation_nonce:
        raise ValueError("generation_nonce must be a non-empty string")
    if not callable(fatal_callback):
        raise TypeError("fatal_callback must be callable")
    _require_drain_wake(_drain_wake)


def _startup_deadline_from(_startup_deadline: object) -> float:
    if _startup_deadline is None:
        return time.monotonic() + _OWNER_JOIN_SECONDS
    return _require_monotonic(_startup_deadline, "_startup_deadline")


def _first_interruption(current, error: BaseException):
    if current is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
        return error
    return current


def _startup_interruption_source(startup_error, cleanup_interruption, cleanup_errors):
    if _interruption_in_chain(startup_error) is not None:
        return startup_error
    if cleanup_interruption is not None:
        return cleanup_interruption
    for error in cleanup_errors:
        if _interruption_in_chain(error) is not None:
            return error
    return None


def _raise_cleanup_with_interruption(ownership_error, startup_error, interruption_source) -> None:
    try:
        raise ownership_error from startup_error
    except _ProtocolStartupCleanupError as retained_error:
        _raise_collected_errors((interruption_source, retained_error))


def _require_method_and_params(method: object, params: object) -> None:
    if not isinstance(method, str) or not method:
        raise ValueError("method must be a non-empty string")
    if not isinstance(params, (dict, list)):
        raise TypeError("params must be an object or array")


def _require_cancellation(cancellation: object) -> None:
    if cancellation is not None and not isinstance(cancellation, CancellationToken):
        raise TypeError("cancellation must be a CancellationToken")


def _require_reason(reason: object) -> None:
    if not isinstance(reason, str) or not reason or len(reason.encode("utf-8")) > 256:
        raise ValueError("reason must be a non-empty string of at most 256 bytes")


def _close_deadline(deadline: object) -> float:
    if deadline is None:
        return time.monotonic() + _OWNER_JOIN_SECONDS
    return _require_monotonic(deadline, "deadline")


def _cancelled_at(cancellation: CancellationToken | None) -> float | None:
    if cancellation is None:
        return None
    return cancellation.cancelled_at


def _effective_cancellation(pending: PendingRequest, at: float) -> float | None:
    """The cancellation instant when it precedes both the deadline and `at`."""
    cancelled_at = _cancelled_at(pending.cancellation)
    if cancelled_at is None or cancelled_at > pending.deadline or cancelled_at > at:
        return None
    return cancelled_at


def _drain_expired(pending: PendingRequest, now: float) -> bool:
    return pending.drain_deadline is not None and now >= pending.drain_deadline


def _mark_terminal(request: PendingRequest, terminal: str, at: float, source: str, error) -> None:
    if request.terminal is not None:
        return
    request.terminal = terminal
    request.terminal_at = at
    request.terminal_source = source
    request.terminal_error = error


def _complete_all(pending, writes, error: BaseException) -> None:
    for request in pending:
        request.completed.set()
    for task in writes:
        if task.error is None:
            task.error = error
        task.completed.set()


def _fatal_error_from(reason: str, cause: BaseException | None) -> ProtocolViolation:
    if isinstance(cause, ProtocolViolation):
        return cause
    error = ProtocolViolation(reason)
    if cause is not None:
        error.__cause__ = cause
    return error


def _raise_terminal_outcome(pending: PendingRequest, outcome: str) -> None:
    if outcome == "fatal":
        assert pending.terminal_error is not None
        raise pending.terminal_error
    if outcome == "closed":
        raise ProtocolViolation("LSP protocol is closed")


def _require_location_count(result: object) -> None:
    if isinstance(result, list) and len(result) > MAX_LOCATIONS:
        raise ProtocolViolation("location result exceeds 10,000 items")


def _require_hover_size(result: object) -> None:
    try:
        size = len(
            json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProtocolViolation("hover result is not strict JSON") from exc
    if size > MAX_HOVER_BYTES:
        raise ProtocolViolation("hover result exceeds 256 KiB")


def _require_unseen_symbol(symbol: dict, seen: set[int]) -> None:
    identity = id(symbol)
    if identity in seen:
        raise ProtocolViolation("document symbol result contains a cycle")
    seen.add(identity)


def _push_symbol_children(symbol: dict, stack: list[object], count: int) -> None:
    children = symbol.get("children")
    if not isinstance(children, list):
        return
    if count + len(stack) + len(children) > MAX_LOCATIONS:
        raise ProtocolViolation("location result exceeds 10,000 items")
    stack.extend(children)


def _walk_document_symbols(result: list) -> None:
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
        _require_unseen_symbol(symbol, seen)
        _push_symbol_children(symbol, stack, count)


def _call_quietly(function: Callable[[Any], object], argument: object) -> None:
    try:
        function(argument)
    except BaseException:
        pass


def _shutdown_socket(sock: object) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(2)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _close_layer(layer: object) -> bool:
    close = getattr(layer, "close", None)
    if close is None:
        return False
    try:
        close()
    except (OSError, ValueError):
        pass
    return True


def _owner_never_started(owner: threading.Thread) -> bool:
    return owner.ident is None and owner not in threading.enumerate()


def _owner_present(owner: threading.Thread) -> bool:
    return owner.ident is not None or owner in threading.enumerate()


def _require_owner_stopped(owner: threading.Thread) -> None:
    if owner.is_alive():
        raise TimeoutError("LSP protocol owner did not stop during startup cleanup")


def _writable_within(descriptor: int, deadline: float) -> bool:
    remaining = min(0.05, max(0.0, deadline - time.monotonic()))
    _, writable, _ = select.select([], [descriptor], [], remaining)
    return bool(writable)


def _write_some(descriptor: int, view: memoryview) -> int:
    try:
        written = os.write(descriptor, view)
    except BlockingIOError:
        return 0
    if written <= 0:
        raise OSError("LSP writer made no progress")
    return written


def _restore_blocking(descriptor: int, was_blocking: bool) -> None:
    try:
        os.set_blocking(descriptor, was_blocking)
    except OSError:
        pass


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
        _require_protocol_arguments(generation_nonce, fatal_callback, _drain_wake)
        startup_deadline = _startup_deadline_from(_startup_deadline)
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
            self._start_owners(startup_deadline)
        except BaseException as startup_error:
            self._abort_startup(startup_error, startup_deadline)

    def _start_owners(self, startup_deadline: float) -> None:
        self._require_owner_start_budget(startup_deadline)
        self.writer_thread.start()
        self._require_owner_start_budget(startup_deadline)
        self.reader_thread.start()
        self._wait_owner_registration(self._writer_started, "writer", startup_deadline)
        self._wait_owner_registration(self._reader_started, "reader", startup_deadline)
        self._raise_owner_start_error()

    def _put_stop_sentinel(self) -> None:
        try:
            self._write_queue.put_nowait(None)
        except queue.Full:
            pass

    def _cancel_started_owners_io(self) -> None:
        for owner in (self.reader_thread, self.writer_thread):
            if owner.ident is not None:
                self._cancel_owner_io(owner)

    def _join_owners_after_failed_start(self, deadline: float) -> tuple[list[BaseException], object]:
        cleanup_errors: list[BaseException] = []
        cleanup_interruption = None
        for owner in (self.reader_thread, self.writer_thread):
            try:
                self._join_partially_started_owner(owner, deadline)
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
                cleanup_interruption = _first_interruption(cleanup_interruption, cleanup_error)
        return cleanup_errors, cleanup_interruption

    def _abort_startup(self, startup_error: BaseException, startup_deadline: float) -> None:
        """Undo a failed start: stop the owners that did start, then raise what happened."""
        self._io_stopped.set()
        self._put_stop_sentinel()
        self._cancel_started_owners_io()
        self._interrupt_stream(self._reader)
        self._interrupt_stream(self._writer)
        cleanup_errors, cleanup_interruption = self._join_owners_after_failed_start(startup_deadline)
        if not cleanup_errors:
            _raise_collected_errors((startup_error,))
        ownership_error = _ProtocolStartupCleanupError(self, tuple(cleanup_errors))
        interruption_source = _startup_interruption_source(
            startup_error, cleanup_interruption, cleanup_errors
        )
        if interruption_source is not None:
            _raise_cleanup_with_interruption(ownership_error, startup_error, interruption_source)
        raise ownership_error from startup_error

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
        now = _require_monotonic(now, "now")
        with self._state_lock:
            return tuple(
                sorted(key for key, pending in self._pending.items() if _drain_expired(pending, now))
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

    def _require_request_admissible_locked(
        self, method: str, deadline: float, cancellation: CancellationToken | None
    ) -> None:
        _require_not_cancelled_or_late(method, deadline, _cancelled_at(cancellation), time.monotonic())
        if len(self._pending) >= MAX_PENDING_REQUESTS:
            raise PendingRequestLimitExceeded("at most 32 LSP requests may be active")

    def _register_request(
        self, method: str, deadline: float, cancellation: CancellationToken | None
    ) -> tuple[tuple[str, int], PendingRequest]:
        with self._state_lock:
            self._raise_if_unavailable_locked()
            self._require_request_admissible_locked(method, deadline, cancellation)
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
        return key, pending

    def _wait_for_completion(
        self, pending: PendingRequest, remaining: float, cancellation: CancellationToken | None
    ) -> None:
        wait_for = max(0.0, remaining)
        if cancellation is not None:
            wait_for = min(wait_for, _CANCELLATION_POLL_SECONDS)
        if pending.completed.wait(wait_for):
            time.sleep(0)

    def _await_outcome(
        self,
        key: tuple[str, int],
        pending: PendingRequest,
        deadline: float,
        cancellation: CancellationToken | None,
    ) -> str:
        while True:
            now = time.monotonic()
            with self._state_lock:
                self._commit_due_locked(key, pending, now, source="requester")
                outcome = pending.terminal
            if outcome is not None:
                return outcome
            self._wait_for_completion(pending, deadline - now, cancellation)

    def _raise_timed_out(self, pending: PendingRequest, method: str) -> None:
        if pending.write_phase == "sending":
            error = TimeoutError("LSP write exceeded its deadline")
            self._become_fatal("LSP write exceeded its deadline", cause=error)
        raise TimeoutError(f"LSP request timed out: {method}")

    def _request_result(self, pending: PendingRequest, method: str, outcome: str) -> object:
        if outcome == "cancelled":
            raise RequestCancelled(f"LSP request cancelled: {method}")
        if outcome == "timed_out":
            self._raise_timed_out(pending, method)
        _raise_terminal_outcome(pending, outcome)
        return _response_value(pending)

    def request(
        self,
        method: str,
        params: object,
        *,
        deadline: float,
        cancellation: CancellationToken | None = None,
    ) -> object:
        _require_method_and_params(method, params)
        deadline = _require_monotonic(deadline, "deadline")
        _require_cancellation(cancellation)
        key, pending = self._register_request(method, deadline, cancellation)
        try:
            self._queue_request_message(
                {"jsonrpc": "2.0", "id": pending.request_id, "method": method, "params": params},
                deadline=deadline,
                key=key,
            )
        except BaseException:
            self._abandon_pending(key, pending)
            raise
        outcome = self._await_outcome(key, pending, deadline, cancellation)
        return self._request_result(pending, method, outcome)

    def notify(self, method: str, params: object, *, deadline: float) -> None:
        """Deliver one notification through the single owned writer."""
        _require_method_and_params(method, params)
        deadline = _require_monotonic(deadline, "deadline")
        self._write_message(
            {"jsonrpc": "2.0", "method": method, "params": params},
            deadline=deadline,
            wait=True,
        )

    def _cancel_active_locked(self, now: float) -> list[PendingRequest]:
        completed: list[PendingRequest] = []
        for key, pending in tuple(self._pending.items()):
            if pending.terminal is None:
                self._commit_local_locked(key, pending, "cancelled", now, "manager")
                completed.append(pending)
        return completed

    def cancel_all(self, reason: str) -> None:
        """Cancel every active request without bypassing writer ownership."""
        _require_reason(reason)
        now = time.monotonic()
        with self._state_lock:
            self._raise_if_unavailable_locked()
            completed = self._cancel_active_locked(now)
        for pending in completed:
            pending.completed.set()

    def _stop_io(self) -> None:
        self._io_stopped.set()
        self._cancel_owner_io(self.reader_thread)
        self._cancel_owner_io(self.writer_thread)
        self._interrupt_stream(self._reader)
        self._interrupt_stream(self._writer)
        self._put_stop_sentinel()

    def _reset_write_accounting(self) -> None:
        with self._state_lock:
            self._write_tasks.clear()
            self._ordinary_queued = 0
            self._control_queued = 0

    def _drain_write_queue(self) -> None:
        while True:
            try:
                self._write_queue.get_nowait()
            except queue.Empty:
                return

    def close(self, deadline: float | None = None) -> None:
        deadline = _close_deadline(deadline)
        self._close_logically()
        self._stop_io()
        self._join_owners(deadline)
        self._reset_write_accounting()
        self._drain_write_queue()

    def _terminate_pending_locked(
        self, terminal: str, error: BaseException, source: str
    ) -> tuple[PendingRequest, ...]:
        """Commit what is due, mark the rest `terminal`, and empty the table."""
        pending_items = tuple(self._pending.items())
        at = time.monotonic()
        for key, request in pending_items:
            self._commit_due_locked(key, request, at, source=source)
            _mark_terminal(request, terminal, at, source, error)
        self._pending.clear()
        return tuple(request for _key, request in pending_items)

    def _close_logically(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            closed_error = ProtocolViolation("LSP protocol is closed")
            pending = self._terminate_pending_locked("closed", closed_error, "transport")
            writes = tuple(self._write_tasks)
        _complete_all(pending, writes, closed_error)

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

    def _stopped_locked(self) -> bool:
        return self._closed or self._fatal_error is not None

    def _is_stopped(self) -> bool:
        with self._state_lock:
            return self._stopped_locked()

    def _is_closed(self) -> bool:
        with self._state_lock:
            return self._closed

    def _fail_unless_closed(self, reason: str, exc: BaseException) -> None:
        if not self._is_closed():
            self._become_fatal(reason, cause=exc)

    def _read_until_stopped(self, frame_reader: JsonRpcFrameReader) -> None:
        while not self._is_stopped():
            message = frame_reader.read()
            self._dispatch_message(message, generation_nonce=self.generation_nonce)

    def _reader_loop(self) -> None:
        if not self._register_owner("reader", self._reader_started):
            return
        self.stdout_reader_owner = threading.get_ident()
        frame_reader = JsonRpcFrameReader(_OwnedReader(self._reader, self._io_stopped))
        try:
            self._read_until_stopped(frame_reader)
        except ProtocolViolation as exc:
            self._fail_unless_closed(str(exc), exc)
        except (OSError, ValueError) as exc:
            self._fail_unless_closed("failed to read LSP stdout", exc)
        finally:
            self._close_stream(self._reader)
            self._release_owner("reader")

    def _uncount_queued_locked(self, task: _WriteTask) -> None:
        if task.control:
            self._control_queued -= 1
            return
        self._ordinary_queued -= 1

    def _pending_for_locked(self, task: _WriteTask) -> PendingRequest | None:
        if task.request_key is None:
            return None
        return self._pending.get(task.request_key)

    def _skip_request_locked(self, task: _WriteTask, pending: PendingRequest | None) -> bool:
        if task.request_key is None:
            return False
        if task.request_key in self._cancelled_keys:
            return True
        return pending is None or pending.terminal is not None

    def _take_task_locked(self, task: _WriteTask) -> tuple[bool, bool, str | None]:
        """(protocol stopped, request to skip, method now sending) for one dequeued task."""
        with self._state_lock:
            self._uncount_queued_locked(task)
            stopped = self._stopped_locked()
            pending = self._pending_for_locked(task)
            if pending is not None and pending.write_phase == "queued":
                self._commit_due_locked(task.request_key, pending, time.monotonic(), source="writer")
            skip_request = self._skip_request_locked(task, pending)
            request_method = None
            if pending is not None and not skip_request:
                pending.write_phase = "sending"
                request_method = pending.method
        return stopped, skip_request, request_method

    def _write_deadline_outcome(self, task: _WriteTask, expired: bool, message: str) -> bool | None:
        """None while the deadline holds; True to go on (best effort); False to stop."""
        if not expired:
            return None
        if task.best_effort:
            self._complete_write(task, None)
            return True
        error = TimeoutError(message)
        self._become_fatal(message, cause=error)
        self._complete_write(task, error)
        return False

    def _try_write_frame(self, task: _WriteTask) -> bool:
        try:
            self._write_frame(task)
        except BaseException as exc:
            self._fail_unless_closed("failed to write LSP message", exc)
            self._complete_write(task, exc)
            return False
        return True

    def _note_sent_request(self, request_method: str | None) -> None:
        if request_method is None:
            return
        with self._state_lock:
            self._sent_request_sequence += 1
            self._last_sent_request_method = request_method

    def _mark_sent(self, task: _WriteTask) -> None:
        with self._state_lock:
            pending = self._pending_for_locked(task)
            if pending is None:
                return
            pending.write_phase = "sent"
            if pending.terminal in {"cancelled", "timed_out"}:
                self._enqueue_cancel_locked(pending)

    def _finish_written_task(self, task: _WriteTask, request_method: str | None) -> bool:
        self._note_sent_request(request_method)
        outcome = self._write_deadline_outcome(
            task, time.monotonic() > task.deadline, "LSP write exceeded its deadline"
        )
        if outcome is not None:
            return outcome
        self._mark_sent(task)
        self._complete_write(task, None)
        return True

    def _process_write_task(self, task: _WriteTask) -> bool:
        """Write one dequeued task; False when the writer loop must end."""
        stopped, skip_request, request_method = self._take_task_locked(task)
        dequeued = self._dequeued_outcome(task, stopped, skip_request)
        if dequeued is not None:
            return dequeued
        return self._write_live_task(task, request_method)

    def _dequeued_outcome(self, task: _WriteTask, stopped: bool, skip_request: bool) -> bool | None:
        """The loop's answer for a task that must not be written, or None to write it."""
        if stopped:
            self._complete_write(task, ProtocolViolation("LSP protocol stopped"))
            return False
        if skip_request:
            self._complete_write(task, None)
            return True
        return None

    def _write_live_task(self, task: _WriteTask, request_method: str | None) -> bool:
        outcome = self._write_deadline_outcome(
            task, time.monotonic() >= task.deadline, "LSP write deadline expired"
        )
        if outcome is not None:
            return outcome
        if not self._try_write_frame(task):
            return False
        return self._finish_written_task(task, request_method)

    def _write_next(self) -> bool:
        task = self._write_queue.get()
        if task is None:
            return False
        return self._process_write_task(task)

    def _writer_loop(self) -> None:
        if not self._register_owner("writer", self._writer_started):
            return
        self.stdin_writer_owner = threading.get_ident()
        try:
            while self._write_next():
                pass
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

    def _store_response_locked(
        self, pending: PendingRequest, message: dict[str, Any]
    ) -> ProtocolViolation | None:
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
            return exc
        return None

    def _settle_response_locked(self, key: tuple[str, int], pending: PendingRequest) -> None:
        responded_at = time.monotonic()
        pending.responded_at = responded_at
        if pending.terminal is None:
            self._commit_response_locked(key, pending, responded_at)
        elif pending.terminal in {"cancelled", "timed_out"}:
            self._forget_as_cancelled_locked(key)
        pending.completed.set()

    def _accept_response(
        self, key: tuple[str, int], message: dict[str, Any]
    ) -> ProtocolViolation | None:
        with self._state_lock:
            if key in self._responded_keys:
                return ProtocolViolation("duplicate active response ID")
            return self._accept_pending_locked(key, message)

    def _accept_pending_locked(
        self, key: tuple[str, int], message: dict[str, Any]
    ) -> ProtocolViolation | None:
        pending = self._pending.get(key)
        if pending is None:
            return None
        violation = self._store_response_locked(pending, message)
        if violation is None:
            self._settle_response_locked(key, pending)
        return violation

    def _handle_response(self, message: dict[str, Any], generation_nonce: str) -> None:
        response_id = message["id"]
        if isinstance(response_id, str):
            self._become_fatal("server response ID does not match an active request")
            return
        violation = self._accept_response((generation_nonce, response_id), message)
        if violation is not None:
            self._become_fatal(str(violation), cause=violation)

    def _validate_result(self, method: str, result: object) -> None:
        if method in _FLAT_SEMANTIC_RESULT_METHODS:
            _require_location_count(result)
        elif method == "textDocument/documentSymbol":
            self._validate_document_symbol_count(result)
        if method == "textDocument/hover":
            _require_hover_size(result)

    @staticmethod
    def _validate_document_symbol_count(result: object) -> None:
        if not isinstance(result, list):
            return
        if len(result) > MAX_LOCATIONS:
            raise ProtocolViolation("location result exceeds 10,000 items")
        _walk_document_symbols(result)

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

    def _warn_unknown_notification(self) -> None:
        callback: Callable[[str], None] | None = None
        with self._state_lock:
            if not self._unknown_notification_warned:
                self._unknown_notification_warned = True
                callback = self._warning_callback
        if callback is not None:
            _call_quietly(callback, _UNKNOWN_NOTIFICATION_WARNING)

    def _diagnostics_are_bounded(self, params: object) -> bool:
        if not isinstance(params, dict) or not isinstance(params.get("diagnostics"), list):
            self._become_fatal("diagnostic notification has invalid shape")
            return False
        if len(params["diagnostics"]) > MAX_DIAGNOSTICS:
            self._become_fatal("diagnostic notification exceeds 10,000 items")
            return False
        return True

    def _handle_server_notification(self, message: dict[str, Any]) -> None:
        method = message["method"]
        if method not in SERVER_NOTIFICATIONS:
            self._warn_unknown_notification()
            return
        self._dispatch_known_notification(method, message.get("params"))

    def _dispatch_known_notification(self, method: str, params: object) -> None:
        if method == "textDocument/publishDiagnostics" and not self._diagnostics_are_bounded(params):
            return
        handler = self._server_notification_handlers.get(method)
        if handler is not None:
            _call_quietly(handler, params)

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
        self._commit_expired_locked(key, pending, now, source)

    def _commit_expired_locked(
        self, key: tuple[str, int], pending: PendingRequest, now: float, source: str
    ) -> None:
        cancelled_at = _effective_cancellation(pending, now)
        if cancelled_at is not None:
            self._commit_local_locked(key, pending, "cancelled", cancelled_at, source)
            return
        if pending.deadline <= now:
            self._commit_local_locked(key, pending, "timed_out", pending.deadline, source)

    def _forget_as_cancelled_locked(self, key: tuple[str, int]) -> None:
        self._pending.pop(key, None)
        self._remember_key(key, self._cancelled_keys, self._cancelled_order)

    def _commit_response_locked(
        self,
        key: tuple[str, int],
        pending: PendingRequest,
        responded_at: float,
    ) -> None:
        cancelled_at = _effective_cancellation(pending, responded_at)
        if cancelled_at is not None:
            self._commit_local_locked(key, pending, "cancelled", cancelled_at, "reader")
            self._forget_as_cancelled_locked(key)
            return
        if pending.deadline <= responded_at:
            self._commit_local_locked(key, pending, "timed_out", pending.deadline, "reader")
            self._forget_as_cancelled_locked(key)
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
                self._forget_as_cancelled_locked(key)

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

    def _raise_write_timeout(self, message: str) -> None:
        error = TimeoutError(message)
        self._become_fatal(message, cause=error)
        raise error

    def _enqueue_or_fail(self, task: _WriteTask) -> None:
        with self._state_lock:
            self._raise_if_unavailable_locked()
            queue_full = not self._enqueue_write_locked(task)
        if not queue_full:
            return
        exc = queue.Full()
        self._complete_write(task, exc)
        self._become_fatal("LSP write queue deadline expired", cause=exc)
        raise TimeoutError("LSP write queue deadline expired") from exc

    def _raise_write_failure(self, task_error: BaseException) -> None:
        with self._state_lock:
            fatal_error = self._fatal_error
            closed = self._closed
        if fatal_error is not None:
            raise fatal_error
        if closed:
            raise ProtocolViolation("LSP protocol is closed")
        raise ProtocolViolation("failed to write LSP message") from task_error

    def _await_write(self, task: _WriteTask, deadline: float) -> None:
        remaining = max(0.0, deadline - time.monotonic())
        if not task.completed.wait(remaining):
            self._raise_write_timeout("LSP write exceeded its deadline")
        if task.error is not None:
            self._raise_write_failure(task.error)

    def _write_task(self, message: object, deadline: float) -> _WriteTask:
        frame = encode_frame(message)
        if time.monotonic() >= deadline:
            self._raise_write_timeout("LSP write deadline expired")
        return _WriteTask(frame, deadline, threading.Event())

    def _write_message(
        self,
        message: object,
        *,
        deadline: float | None = None,
        wait: bool = False,
    ) -> None:
        if deadline is None:
            deadline = time.monotonic() + _INTERNAL_WRITE_SECONDS
        task = self._write_task(message, deadline)
        self._enqueue_or_fail(task)
        if wait:
            self._await_write(task, deadline)

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
        self._enqueue_or_fail(_WriteTask(frame, deadline, threading.Event(), request_key=key))

    def _write_slot_available_locked(self, task: _WriteTask) -> bool:
        if task.control:
            return self._ordinary_queued + self._control_queued < _MAX_QUEUED_WRITES
        return self._ordinary_queued < _MAX_ORDINARY_WRITES

    def _count_queued_locked(self, task: _WriteTask) -> None:
        if task.control:
            self._control_queued += 1
            return
        self._ordinary_queued += 1

    def _enqueue_write_locked(self, task: _WriteTask) -> bool:
        if not self._write_slot_available_locked(task):
            return False
        self._write_tasks.append(task)
        try:
            self._write_queue.put_nowait(task)
        except queue.Full as exc:  # pragma: no cover - counters and queue share the lock
            self._write_tasks.remove(task)
            raise ProtocolViolation("LSP write queue accounting invariant breached") from exc
        self._count_queued_locked(task)
        return True

    def _complete_write(self, task: _WriteTask, error: BaseException | None) -> None:
        with self._state_lock:
            if task in self._write_tasks:
                self._write_tasks.remove(task)
            if task.error is None:
                task.error = error
        task.completed.set()

    def _require_write_deadline(self, task: _WriteTask) -> None:
        if self._io_stopped.is_set() or time.monotonic() >= task.deadline:
            raise TimeoutError("LSP write exceeded its deadline")

    def _write_nonblocking(self, descriptor: int, task: _WriteTask) -> None:
        offset = 0
        frame = memoryview(task.frame)
        while offset < len(task.frame):
            self._require_write_deadline(task)
            if not _writable_within(descriptor, task.deadline):
                continue
            offset += _write_some(descriptor, frame[offset:])

    def _write_frame(self, task: _WriteTask) -> None:
        descriptor = None if os.name == "nt" else _descriptor_of(self._writer)
        if descriptor is None:
            self._writer.write(task.frame)
            self._writer.flush()
            return
        was_blocking = os.get_blocking(descriptor)
        os.set_blocking(descriptor, False)
        try:
            self._write_nonblocking(descriptor, task)
        finally:
            _restore_blocking(descriptor, was_blocking)

    def _become_fatal(self, reason: str, *, cause: BaseException | None = None) -> None:
        with self._state_lock:
            if self._fatal_error is not None or self._closed:
                return
            error = _fatal_error_from(reason, cause)
            self._fatal_error = error
            pending = self._terminate_pending_locked("fatal", error, "transport")
            writes = tuple(self._write_tasks)
            callback = self._fatal_callback
        _complete_all(pending, writes, error)
        self._stop_io()
        _call_quietly(callback, reason)

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
        for layer in reversed(cls._stream_layers(stream)):
            _shutdown_socket(getattr(layer, "_sock", None))
            if _close_layer(layer):
                break

    @staticmethod
    def _close_stream(stream: BinaryIO) -> None:
        try:
            stream.close()
        except (OSError, ValueError):
            pass

    def _owner_name(self, owner: threading.Thread) -> str:
        if owner is self.reader_thread:
            return "reader"
        return "writer"

    def _release_and_raise(self, name: str) -> None:
        self._release_owner(name)
        with self._owner_handle_lock:
            release_error = self._owner_release_errors.get(name)
        if release_error is not None:
            raise release_error

    def _owner_stop_timeout(self, name: str) -> TimeoutError:
        error = TimeoutError("LSP protocol owner did not stop before deadline")
        with self._owner_handle_lock:
            interruption = self._owner_interrupt_errors.get(name)
        if interruption is not None:
            error.__cause__ = interruption
        return error

    def _join_owner(self, owner: threading.Thread, deadline: float) -> None:
        if owner is threading.current_thread():
            return
        name = self._owner_name(owner)
        if _owner_never_started(owner):
            self._release_and_raise(name)
            return
        self._join_started_owner(owner, name, deadline)

    def _join_started_owner(self, owner: threading.Thread, name: str, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            owner.join(remaining)
        if owner.is_alive():
            raise self._owner_stop_timeout(name)
        with self._owner_handle_lock:
            self._owner_interrupt_errors.pop(name, None)
        self._release_and_raise(name)

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

    @staticmethod
    def _retry_after_join_error(owner: threading.Thread, deadline: float) -> bool:
        if time.monotonic() >= deadline:
            _require_owner_stopped(owner)
            return False
        time.sleep(min(0.001, max(0.0, deadline - time.monotonic())))
        return True

    def _join_once(self, owner: threading.Thread, deadline: float) -> bool:
        """One join attempt; True to try again, False when the owner is settled."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _require_owner_stopped(owner)
            return False
        try:
            owner.join(remaining)
        except RuntimeError:
            return self._retry_after_join_error(owner, deadline)
        _require_owner_stopped(owner)
        return False

    def _join_partially_started_owner(
        self,
        owner: threading.Thread,
        deadline: float,
    ) -> None:
        while _owner_present(owner):
            if not self._join_once(owner, deadline):
                break
        self._release_and_raise(self._owner_name(owner))

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

    def _os_handle_locked(self, name: str) -> int | None:
        if name == "reader":
            return self._reader_os_handle
        return self._writer_os_handle

    def _clear_os_handle_locked(self, name: str) -> None:
        if name == "reader":
            self._reader_os_handle = None
            return
        self._writer_os_handle = None

    def _release_owner(self, name: str) -> None:
        if os.name != "nt":
            return
        with self._owner_handle_lock:
            handle = self._os_handle_locked(name)
            if handle is None:
                return
            if not _KERNEL32.CloseHandle(handle):
                self._owner_release_errors[name] = ctypes.WinError(ctypes.get_last_error())
                return
            self._owner_release_errors.pop(name, None)
            self._clear_os_handle_locked(name)

    def _record_cancel_outcome_locked(self, name: str, cancelled: object) -> None:
        if cancelled:
            self._owner_interrupt_errors.pop(name, None)
            return
        error_number = ctypes.get_last_error()
        if error_number == _ERROR_NOT_FOUND:
            self._owner_interrupt_errors.pop(name, None)
            return
        self._owner_interrupt_errors[name] = ctypes.WinError(error_number)

    def _cancel_owner_io(self, owner: threading.Thread) -> None:
        if os.name != "nt" or owner is threading.current_thread():
            return
        name = self._owner_name(owner)
        with self._owner_handle_lock:
            handle = self._os_handle_locked(name)
            if handle is None:
                return
            self._record_cancel_outcome_locked(name, _KERNEL32.CancelSynchronousIo(handle))


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
