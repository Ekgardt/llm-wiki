"""Strict and bounded LSP JSON-RPC framing and request coordination."""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, BinaryIO

MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_HEADER_BYTES = 8 * 1024
MAX_PENDING_REQUESTS = 32
MAX_LOCATIONS = 10_000
MAX_DIAGNOSTICS = 10_000
MAX_HOVER_BYTES = 256 * 1024
MAX_JSON_DEPTH = 64
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

_LOCATION_METHODS = frozenset(
    {
        "textDocument/declaration",
        "textDocument/definition",
        "textDocument/documentSymbol",
        "textDocument/implementation",
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


class ProtocolViolation(RuntimeError):
    """A peer sent data outside the bounded LSP protocol contract."""


class RequestCancelled(RuntimeError):
    """The caller cancelled an active LSP request."""


class PendingRequestLimitExceeded(RuntimeError):
    """The connection already has the maximum number of active requests."""


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
    result: object | None = None
    error: JsonRpcError | None = None
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class JsonRpcError:
    code: int
    message: str
    data: object | None


def json_depth(value: object) -> int:
    """Return container nesting depth without using Python recursion."""
    if not isinstance(value, (dict, list)):
        return 0
    maximum = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        maximum = max(maximum, depth)
        children = current.values() if isinstance(current, dict) else current
        for child in children:
            if isinstance(child, (dict, list)):
                stack.append((child, depth + 1))
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
    ) -> None:
        if not isinstance(generation_nonce, str) or not generation_nonce:
            raise ValueError("generation_nonce must be a non-empty string")
        if not callable(fatal_callback):
            raise TypeError("fatal_callback must be callable")
        self._reader = reader
        self._writer = writer
        self.generation_nonce = generation_nonce
        self._fatal_callback = fatal_callback
        self._warning_callback = warning_callback
        self._server_request_handlers = dict(server_request_handlers or {})
        self._server_notification_handlers = dict(server_notification_handlers or {})
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: dict[tuple[str, int], PendingRequest] = {}
        self._cancelled_keys: set[tuple[str, int]] = set()
        self._cancelled_order: deque[tuple[str, int]] = deque()
        self._responded_keys: set[tuple[str, int]] = set()
        self._responded_order: deque[tuple[str, int]] = deque()
        self._next_request_id = 1
        self._fatal_error: ProtocolViolation | None = None
        self._closed = False
        self._unknown_notification_warned = False
        self._reader_started = threading.Event()
        self.stdout_reader_owner: int | None = None
        self.reader_thread = threading.Thread(
            target=self._reader_loop,
            name=f"lsp-stdout-{generation_nonce}",
            daemon=True,
        )
        self.reader_thread.start()
        self._reader_started.wait()

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

    def request(
        self,
        method: str,
        params: object,
        *,
        deadline: float,
        cancelled: Callable[[], bool] | None = None,
    ) -> object:
        if not isinstance(method, str) or not method:
            raise ValueError("method must be a non-empty string")
        if not isinstance(params, (dict, list)):
            raise TypeError("params must be an object or array")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            raise TypeError("deadline must be a monotonic timestamp")
        if not math.isfinite(deadline):
            raise ValueError("deadline must be finite")
        if cancelled is not None and not callable(cancelled):
            raise TypeError("cancelled must be callable")

        with self._state_lock:
            self._raise_if_unavailable_locked()
            if len(self._pending) >= MAX_PENDING_REQUESTS:
                raise PendingRequestLimitExceeded("at most 32 LSP requests may be active")
            request_id = self._allocate_request_id_locked()
            pending = PendingRequest(
                request_id,
                method,
                self.generation_nonce,
                float(deadline),
                threading.Event(),
            )
            key = (self.generation_nonce, request_id)
            self._pending[key] = pending

        try:
            self._write_message(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
        except BaseException as exc:
            self._become_fatal("failed to write LSP request", cause=exc)

        while True:
            remaining = float(deadline) - time.monotonic()
            if remaining <= 0:
                self._cancel_pending(key, pending)
                raise TimeoutError(f"LSP request timed out: {method}")
            if cancelled is not None and cancelled():
                self._cancel_pending(key, pending)
                raise RequestCancelled(f"LSP request cancelled: {method}")
            wait_for = remaining
            if cancelled is not None:
                wait_for = min(wait_for, _CANCELLATION_POLL_SECONDS)
            if pending.completed.wait(wait_for):
                break

        with self._state_lock:
            self._pending.pop(key, None)
            fatal_error = self._fatal_error
            closed = self._closed
            error = pending.error
            result = pending.result
        if fatal_error is not None:
            raise fatal_error
        if closed:
            raise ProtocolViolation("LSP protocol is closed")
        if error is not None:
            raise JsonRpcResponseError(error)
        return result

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            pending = tuple(self._pending.values())
        for request in pending:
            request.completed.set()
        try:
            self._writer.close()
        except OSError:
            pass

    def _allocate_request_id_locked(self) -> int:
        if self._next_request_id > _JSON_RPC_INTEGER_MAX:
            raise ProtocolViolation("JSON-RPC request ID space exhausted")
        request_id = self._next_request_id
        self._next_request_id += 1
        return request_id

    def _raise_if_unavailable_locked(self) -> None:
        if self._fatal_error is not None:
            raise self._fatal_error
        if self._closed:
            raise ProtocolViolation("LSP protocol is closed")

    def _reader_loop(self) -> None:
        self.stdout_reader_owner = threading.get_ident()
        self._reader_started.set()
        frame_reader = JsonRpcFrameReader(self._reader)
        while True:
            with self._state_lock:
                if self._closed:
                    return
            try:
                message = frame_reader.read()
                self._dispatch_message(message, generation_nonce=self.generation_nonce)
            except ProtocolViolation as exc:
                with self._state_lock:
                    closed = self._closed
                if not closed:
                    self._become_fatal(str(exc), cause=exc)
                return
            except (OSError, ValueError) as exc:
                with self._state_lock:
                    closed = self._closed
                if not closed:
                    self._become_fatal("failed to read LSP stdout", cause=exc)
                return

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
            if key in self._cancelled_keys:
                return
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
                    self._remember_key(
                        key, self._responded_keys, self._responded_order
                    )
                    pending.completed.set()
        if violation is not None:
            self._become_fatal(str(violation), cause=violation)

    def _validate_result(self, method: str, result: object) -> None:
        if method in _LOCATION_METHODS and isinstance(result, list) and len(result) > MAX_LOCATIONS:
            raise ProtocolViolation("location result exceeds 10,000 items")
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

    def _cancel_pending(self, key: tuple[str, int], pending: PendingRequest) -> None:
        with self._state_lock:
            current = self._pending.get(key)
            if current is not pending or pending.completed.is_set():
                return
            pending.cancelled = True
            self._pending.pop(key, None)
            self._remember_key(key, self._cancelled_keys, self._cancelled_order)
        try:
            self._write_message(
                {
                    "jsonrpc": "2.0",
                    "method": "$/cancelRequest",
                    "params": {"id": pending.request_id},
                }
            )
        except BaseException as exc:
            self._become_fatal("failed to write LSP cancellation", cause=exc)

    @staticmethod
    def _remember_key(
        key: tuple[str, int],
        values: set[tuple[str, int]],
        order: deque[tuple[str, int]],
    ) -> None:
        values.add(key)
        order.append(key)
        if len(order) > _TOMBSTONE_LIMIT:
            values.discard(order.popleft())

    def _write_message(self, message: object) -> None:
        frame = encode_frame(message)
        with self._write_lock:
            self._writer.write(frame)
            self._writer.flush()

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
            pending = tuple(self._pending.values())
            callback = self._fatal_callback
        for request in pending:
            request.completed.set()
        try:
            callback(reason)
        except BaseException:
            pass
