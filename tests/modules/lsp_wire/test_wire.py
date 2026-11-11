"""Strict JSON and framing tests, independent of transport close policy."""
from __future__ import annotations

import io
import json
import time

import pytest
from llm_wiki.lsp_wire import (
    MAX_FRAME_BYTES,
    MAX_HEADER_BYTES,
    JsonRpcFrameReader,
    ProtocolViolation,
    encode_frame,
    json_depth,
)


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

