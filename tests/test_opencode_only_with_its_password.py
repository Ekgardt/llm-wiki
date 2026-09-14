"""OpenCode receives a prompt only with its password, on the API its documentation lists.

Any local process answering 200 on port 4096 received the vault's prompts, unauthenticated.
Research: `docs/research/2026-09-14-opencode-only-with-its-password.md`.
"""
from __future__ import annotations

import base64
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import llm_client  # noqa: E402

PASSWORD = "s3cret-for-test"
EXPECTED_AUTH = "Basic " + base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()


class _FakeOpenCode(BaseHTTPRequestHandler):
    seen: list[tuple[str, str, dict]] = []

    def log_message(self, *_args) -> None:
        return

    def _reply(self, status: int, body: object) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorised(self) -> bool:
        return self.headers.get("Authorization") == EXPECTED_AUTH

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _handle(self, routes: dict) -> None:
        body = self._body() if self.command == "POST" else {}
        type(self).seen.append((self.command, self.path, body))
        if not self._authorised():
            return self._reply(401, {"error": "unauthorised"})
        return self._reply(*routes.get(self.path, (404, {})))

    def do_GET(self) -> None:
        self._handle({"/global/health": (200, {"healthy": True, "version": "test"})})

    def do_POST(self) -> None:
        self._handle({
            "/session": (200, {"id": "ses_1"}),
            "/session/ses_1/message": (200, {"info": {}, "parts": [{"type": "text", "text": "the answer"}]}),
        })

    def do_DELETE(self) -> None:
        self._handle({"/session/ses_1": (200, True)})


@pytest.fixture
def server(monkeypatch):
    _FakeOpenCode.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenCode)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("OPENCODE_PORT", str(httpd.server_address[1]))
    monkeypatch.delenv("OPENCODE_SERVER_USERNAME", raising=False)
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def test_without_the_password_opencode_is_not_used_and_nothing_is_sent(server, monkeypatch):
    monkeypatch.delenv("OPENCODE_SERVER_PASSWORD", raising=False)

    assert (llm_client._probe_opencode(None), _FakeOpenCode.seen) == (False, [])


def test_with_the_password_the_documented_api_is_called_authenticated(server, monkeypatch):
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", PASSWORD)

    probed = llm_client._probe_opencode(None)
    answer = llm_client._call_opencode(None, "the question", "be brief")
    calls = [(method, path) for method, path, _body in _FakeOpenCode.seen]
    message = next(body for method, path, body in _FakeOpenCode.seen if path.endswith("/message"))

    assert (probed, answer.text, calls, message["system"]) == (
        True,
        "the answer",
        [("GET", "/global/health"), ("POST", "/session"), ("POST", "/session/ses_1/message"), ("DELETE", "/session/ses_1")],
        "be brief",
    )
