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
_CALLBACK_WORKERS = 2
_MAX_QUEUED_CALLBACKS = MAX_PENDING_REQUESTS
_INTERNAL_WRITE_SECONDS = 1.0
_OWNER_JOIN_SECONDS = 1.0


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


@dataclass(slots=True, eq=False)
class _WriteTask:
    frame: bytes
    deadline: float
    completed: threading.Event
    error: BaseException | None = None


@dataclass(slots=True, eq=False)
class _CallbackTask:
    callback: Callable[[], bool]
    completed: threading.Event
    abandoned: threading.Event
    result: bool = False
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
        self._writer_started = threading.Event()
        self._io_stopped = threading.Event()
        self._callbacks_stopped = threading.Event()
        self._write_queue: queue.Queue[_WriteTask | None] = queue.Queue(
            maxsize=_MAX_QUEUED_WRITES
        )
        self._callback_queue: queue.Queue[_CallbackTask] = queue.Queue(
            maxsize=_MAX_QUEUED_CALLBACKS
        )
        self._write_tasks: list[_WriteTask] = []
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
        self.callback_threads = tuple(
            threading.Thread(
                target=self._callback_loop,
                name=f"lsp-cancel-{generation_nonce}-{index}",
                daemon=True,
            )
            for index in range(_CALLBACK_WORKERS)
        )
        self.writer_thread.start()
        self.reader_thread.start()
        for thread in self.callback_threads:
            thread.start()
        self._writer_started.wait()
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
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                deadline=float(deadline),
                wait=True,
            )
        except TimeoutError:
            self._cancel_pending(key, pending, force=True)
            raise
        except BaseException:
            self._abandon_pending(key, pending)
            raise

        while True:
            remaining = float(deadline) - time.monotonic()
            if remaining <= 0:
                self._cancel_pending(key, pending, force=True)
                raise TimeoutError(f"LSP request timed out: {method}")
            if cancelled is not None:
                try:
                    is_cancelled, callback_timed_out = self._evaluate_cancelled(
                        cancelled, float(deadline)
                    )
                except BaseException:
                    self._cancel_pending(
                        key,
                        pending,
                        force=True,
                    )
                    raise
                if callback_timed_out or time.monotonic() >= deadline:
                    self._cancel_pending(
                        key,
                        pending,
                        force=True,
                    )
                    raise TimeoutError(f"LSP request timed out: {method}")
                if is_cancelled:
                    self._cancel_pending(
                        key,
                        pending,
                        force=True,
                    )
                    raise RequestCancelled(f"LSP request cancelled: {method}")
                remaining = float(deadline) - time.monotonic()
            if pending.completed.is_set():
                break
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
            first_close = not self._closed
            self._closed = True
            pending = tuple(self._pending.values()) if first_close else ()
            writes = tuple(self._write_tasks) if first_close else ()
        if first_close:
            for request in pending:
                request.completed.set()
            closed_error = ProtocolViolation("LSP protocol is closed")
            for task in writes:
                if task.error is None:
                    task.error = closed_error
                task.completed.set()
            self._io_stopped.set()
            self._callbacks_stopped.set()
            self._cancel_owner_io(self.reader_thread)
            self._cancel_owner_io(self.writer_thread)
            self._interrupt_stream(self._reader)
            self._interrupt_stream(self._writer)
            try:
                self._write_queue.put_nowait(None)
            except queue.Full:
                pass
        self._join_owner(self.reader_thread)
        self._join_owner(self.writer_thread)

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

    def _writer_loop(self) -> None:
        self.stdin_writer_owner = threading.get_ident()
        self._writer_started.set()
        try:
            while True:
                task = self._write_queue.get()
                if task is None:
                    return
                with self._state_lock:
                    stopped = self._closed or self._fatal_error is not None
                if stopped:
                    self._complete_write(task, ProtocolViolation("LSP protocol stopped"))
                    return
                if time.monotonic() >= task.deadline:
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
                if time.monotonic() > task.deadline:
                    error = TimeoutError("LSP write exceeded its deadline")
                    self._become_fatal("LSP write exceeded its deadline", cause=error)
                    self._complete_write(task, error)
                    return
                self._complete_write(task, None)
        finally:
            self._close_stream(self._writer)

    def _callback_loop(self) -> None:
        while True:
            try:
                task = self._callback_queue.get(timeout=0.05)
            except queue.Empty:
                if self._callbacks_stopped.is_set():
                    return
                continue
            if self._callbacks_stopped.is_set() or task.abandoned.is_set():
                task.completed.set()
                continue
            try:
                task.result = bool(task.callback())
            except BaseException as exc:
                task.error = exc
            finally:
                task.completed.set()

    def _evaluate_cancelled(
        self,
        callback: Callable[[], bool],
        deadline: float,
    ) -> tuple[bool, bool]:
        if time.monotonic() >= deadline:
            return False, True
        task = _CallbackTask(
            callback,
            threading.Event(),
            threading.Event(),
        )
        try:
            self._callback_queue.put_nowait(task)
        except queue.Full:
            return False, True
        remaining = max(0.0, deadline - time.monotonic())
        if not task.completed.wait(remaining):
            task.abandoned.set()
            return False, True
        if time.monotonic() >= deadline:
            task.abandoned.set()
            return False, True
        if task.error is not None:
            raise task.error
        return task.result, False

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

    def _cancel_pending(
        self,
        key: tuple[str, int],
        pending: PendingRequest,
        *,
        force: bool = False,
    ) -> bool:
        with self._state_lock:
            current = self._pending.get(key)
            if current is not pending:
                return pending.cancelled
            if pending.completed.is_set() and not force:
                self._pending.pop(key, None)
                return False
            pending.cancelled = True
            self._pending.pop(key, None)
            self._responded_keys.discard(key)
            self._remember_key(key, self._cancelled_keys, self._cancelled_order)
        try:
            self._write_message(
                {
                    "jsonrpc": "2.0",
                    "method": "$/cancelRequest",
                    "params": {"id": pending.request_id},
                },
                deadline=time.monotonic() + _INTERNAL_WRITE_SECONDS,
                wait=False,
            )
        except BaseException:
            pass
        return True

    def _abandon_pending(self, key: tuple[str, int], pending: PendingRequest) -> None:
        with self._state_lock:
            if self._pending.get(key) is pending:
                pending.cancelled = True
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
            self._write_tasks.append(task)
            try:
                self._write_queue.put_nowait(task)
            except queue.Full:
                self._write_tasks.remove(task)
                queue_full = True
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
            pending = tuple(self._pending.values())
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

    @staticmethod
    def _join_owner(owner: threading.Thread) -> None:
        if owner is threading.current_thread():
            return
        owner.join(_OWNER_JOIN_SECONDS)

    @staticmethod
    def _cancel_owner_io(owner: threading.Thread) -> None:
        if os.name != "nt" or owner is threading.current_thread() or owner.native_id is None:
            return
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenThread.restype = ctypes.c_void_p
        handle = kernel32.OpenThread(0x0001, False, owner.native_id)
        if not handle:
            return
        try:
            kernel32.CancelSynchronousIo(ctypes.c_void_p(handle))
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
