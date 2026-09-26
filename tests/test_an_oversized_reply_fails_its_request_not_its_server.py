"""An oversized reply fails its own request; the connection keeps answering (audit C-38).

docs/research/2026-09-25-an-oversized-reply-fails-its-request-not-its-server.md
"""
from __future__ import annotations

import io
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from lsp_protocol import (  # noqa: E402
    MAX_FRAME_BYTES,
    FrameTooLarge,
    JsonRpcFrameReader,
    ResponseRefused,
)

from tests.fake_lsp_server import FakeLspPeer, FakeLspServer  # noqa: E402
from tests.slow_machine import LONG_TIMEOUT  # noqa: E402

PADDING = b"x" * (MAX_FRAME_BYTES + 1)


def _frame(body: bytes) -> bytes:
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def _id_first(request_id: int) -> bytes:
    return b'{"jsonrpc":"2.0","id":%d,"result":"' % request_id + PADDING + b'"}'


def _id_last(request_id: int) -> bytes:
    return b'{"jsonrpc":"2.0","result":"' + PADDING + b'","id":%d}' % request_id


@pytest.fixture
def fake_server() -> FakeLspServer:
    server = FakeLspServer()
    yield server
    server.close()


def _oversized_then_normal(shape):
    def handler(peer: FakeLspPeer) -> None:
        first = peer.read()
        peer.send_raw(_frame(shape(first["id"])))
        second = peer.read()
        peer.send({"jsonrpc": "2.0", "id": second["id"], "result": "fine"})

    return handler


def _ask(protocol, method: str = "textDocument/references"):
    return protocol.request(method, {}, deadline=time.monotonic() + LONG_TIMEOUT)


@pytest.mark.parametrize("shape", [_id_first, _id_last])
def test_an_oversized_response_refuses_only_its_request(fake_server, shape):
    protocol = fake_server.start(_oversized_then_normal(shape))

    with pytest.raises(ResponseRefused, match="8 MiB"):
        _ask(protocol)

    assert (protocol.fatal, _ask(protocol)) == (False, "fine")


def test_an_oversized_notification_is_dropped_with_a_warning(fake_server):
    warnings: list[str] = []

    def handler(peer: FakeLspPeer) -> None:
        request = peer.read()
        peer.send_raw(_frame(b'{"jsonrpc":"2.0","method":"$/progress","params":"' + PADDING + b'"}'))
        peer.send({"jsonrpc": "2.0", "id": request["id"], "result": "fine"})

    protocol = fake_server.start(handler, warning_callback=warnings.append)

    assert (_ask(protocol), protocol.fatal) == ("fine", False)
    assert any("oversized" in warning for warning in warnings)


def test_the_reader_consumes_an_oversized_frame_whole():
    stream = io.BytesIO(_frame(_id_last(7)) + _frame(b'{"jsonrpc":"2.0","method":"x"}'))
    reader = JsonRpcFrameReader(stream)

    with pytest.raises(FrameTooLarge) as refused:
        reader.read()

    assert (refused.value.response_id, reader.read()["method"]) == (7, "x")
