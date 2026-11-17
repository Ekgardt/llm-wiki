"""Strict bounded LSP JSON-RPC byte codec; streams remain caller-owned."""
from __future__ import annotations

import json
import math
import re
from typing import Any, BinaryIO

from bounded_io import IO_CHUNK_BYTES

# One JSON-RPC frame: a project constraint of the 2026-07-22 Pyright plan
# (docs/superpowers/plans/2026-07-22-python-pyright-navigation.md, "8 MiB frames"). An oversized
# reply fails its request, not the server. Not measured against real replies.
MAX_FRAME_BYTES = 8 * 1024 * 1024
# A frame over MAX_FRAME_BYTES and up to this is consumed and refused, not fatal
# (audit C-38, docs/research/2026-09-25-an-oversized-reply-fails-its-request-not-its-server.md).
MAX_SKIPPED_FRAME_BYTES = 256 * 1024 * 1024
# One read while draining an oversized frame; the shared I/O step size (bounded_io).
_SKIP_CHUNK_BYTES = IO_CHUNK_BYTES
# The bytes kept from each end of a refused oversized frame, enough for the
# `"jsonrpc":"2.0","id":…` of either field order (`_oversized_response_id`); the frame
# itself is refused whole, so nothing is cut from an answer.
_FRAME_EDGE_BYTES = 256
_HEAD_RESPONSE_ID = re.compile(rb'\A\s*\{\s*(?:"jsonrpc"\s*:\s*"2\.0"\s*,\s*)?"id"\s*:\s*(\d{1,15})\s*,')
_TAIL_RESPONSE_ID = re.compile(rb'[,{]\s*"id"\s*:\s*(\d{1,15})\s*\}\s*\Z')
# LSP headers are two short lines (Content-Length, Content-Type); 8 KiB refuses a
# peer that never ends its header block. Security bound on untrusted input.
MAX_HEADER_BYTES = 8 * 1024

# LSP 3.17 base type `integer`: "a signed integer number in the range of -2^31 to 2^31 - 1".
_JSON_RPC_INTEGER_MIN = -(2**31)
JSON_RPC_INTEGER_MAX = 2**31 - 1

_MAX_JSON_VALUES = MAX_FRAME_BYTES // 2

# Plan constraint of 2026-07-22: JSON nesting 64; replies past it are refused,
# not cut. Not measured against real replies.
MAX_JSON_DEPTH = 64


class ProtocolViolation(RuntimeError):
    """A peer sent data outside the bounded LSP protocol contract."""


class FrameTooLarge(ProtocolViolation):
    """A frame over the frame bound was consumed whole; the stream is still framed.

    `response_id` is the id of the response it carried, when its edges name one.
    """

    def __init__(self, response_id: int | None) -> None:
        super().__init__("LSP frame exceeds 8 MiB")
        self.response_id = response_id


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
        return _JSON_RPC_INTEGER_MIN <= value <= JSON_RPC_INTEGER_MAX
    return isinstance(value, str)


def _error_shape_is_valid(value: dict) -> bool:
    return set(value) <= {"code", "message", "data"} and {"code", "message"} <= set(value)


def _valid_error_code(code: object) -> bool:
    if isinstance(code, bool) or not isinstance(code, int):
        return False
    return _JSON_RPC_INTEGER_MIN <= code <= JSON_RPC_INTEGER_MAX


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
    if len(length_text) > len(str(MAX_SKIPPED_FRAME_BYTES)):
        raise ProtocolViolation("Content-Length exceeds the frame limit")


def _content_length(headers: dict[str, str]) -> int:
    length_text = headers.get("content-length")
    _require_length_text(length_text)
    length = int(length_text)
    if length > MAX_SKIPPED_FRAME_BYTES:
        raise ProtocolViolation("Content-Length exceeds the frame limit")
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


def _skipped_frame_edges(stream: BinaryIO, length: int) -> tuple[bytes, bytes]:
    """Consume an oversized body in bounded chunks, keeping only its two edges."""
    head = b""
    tail = b""
    remaining = length
    while remaining:
        chunk = stream.read(min(_SKIP_CHUNK_BYTES, remaining))
        if not chunk:
            raise ProtocolViolation("unexpected EOF in LSP body")
        remaining -= len(chunk)
        if len(head) < _FRAME_EDGE_BYTES:
            head = (head + chunk)[:_FRAME_EDGE_BYTES]
        tail = (tail + chunk)[-_FRAME_EDGE_BYTES:]
    return head, tail


def _oversized_response_id(head: bytes, tail: bytes) -> int | None:
    """The id of the response an oversized frame carried, when its edges name it.

    A frame whose head names a method is a request or notification. A response's
    id is the first key after `jsonrpc` (vscode-jsonrpc, lsp-server) or the last
    key (Go's field order); anything else is not recovered.
    """
    if b'"method"' in head:
        return None
    match = _HEAD_RESPONSE_ID.search(head) or _TAIL_RESPONSE_ID.search(tail)
    return None if match is None else int(match.group(1))


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
        if length > MAX_FRAME_BYTES:
            raise FrameTooLarge(_oversized_response_id(*_skipped_frame_edges(self._stream, length)))
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


# Existing public imports and pickle names remain stable through lsp_protocol.
ProtocolViolation.__module__ = "lsp_protocol"
FrameTooLarge.__module__ = "lsp_protocol"
JsonRpcFrameReader.__module__ = "lsp_protocol"
encode_frame.__module__ = "lsp_protocol"
json_depth.__module__ = "lsp_protocol"
