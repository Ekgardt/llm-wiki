"""Provider routing must not transfer authorization to a redirect or loopback proxy."""
from __future__ import annotations

import threading
import urllib.error
import urllib.request
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import choose_model
import llm_client
import pytest


class _Handler(BaseHTTPRequestHandler):
    def _reply(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.seen.append((self.command, self.path, self.headers.get("Authorization"), body))
        self.send_response(self.server.status)
        if self.server.location is not None:
            self.send_header("Location", self.server.location)
        self.end_headers()
        self.wfile.write(b'{"healthy":true}')

    do_GET = _reply
    do_POST = _reply
    do_DELETE = _reply

    def log_message(self, *_args):
        return  # Synthetic fixture traffic has no operational log.


@contextmanager
def _server(status=200):
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.status, server.location, server.seen = status, None, []
    server.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.fixture
def servers(monkeypatch):
    # urllib caches proxy discovery in its global opener; each environment
    # scenario must begin with a fresh opener and only its controlled proxy.
    monkeypatch.setattr(urllib.request, "_opener", None)
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv("OPENCODE_SERVER_USERNAME", "probe")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "nonsecret-local-test-value")
    with ExitStack() as stack:
        yield lambda status=200: stack.enter_context(_server(status))


def _request(origin, method):
    calls = {
        "GET": llm_client._opencode_healthy,
        "POST": lambda: llm_client._opencode_post(origin.url + "/session", {"probe": "synthetic"}),
    }
    return calls[method]()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("same_origin", [False, True])
def test_provider_redirect_cannot_send_another_request(servers, monkeypatch, status, method, same_origin):
    origin, other = servers(status), servers()
    target = origin if same_origin else other
    origin.location = target.url + "/unapproved-path"
    monkeypatch.setenv("OPENCODE_PORT", str(origin.server_port))

    with pytest.raises(urllib.error.HTTPError) as caught:
        _request(origin, method)

    assert caught.value.code == status
    assert len(origin.seen) == 1
    assert other.seen == []


def test_model_listing_does_not_forward_bearer_authorization(servers):
    origin, other = servers(302), servers()
    origin.location = other.url + "/unapproved-models"
    request = urllib.request.Request(origin.url + "/models", headers={"Authorization": "Bearer synthetic"})

    with pytest.raises(urllib.error.HTTPError):
        choose_model._fetched_json(request)

    assert len(origin.seen) == 1
    assert other.seen == []


def test_cleanup_redirect_is_reported_without_contacting_its_target(servers, capsys):
    origin, other = servers(302), servers()
    origin.location = other.url + "/unapproved-session"

    llm_client._opencode_delete(origin.url, "synthetic-session")

    assert "OpenCode session cleanup failed" in capsys.readouterr().err
    assert len(origin.seen) == 1
    assert other.seen == []


def _proxy_environment(monkeypatch, proxy):
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    monkeypatch.setenv("http_proxy", proxy.url)


def test_literal_loopback_contacts_origin_without_environment_proxy(servers, monkeypatch):
    origin, proxy = servers(), servers()
    _proxy_environment(monkeypatch, proxy)
    monkeypatch.setenv("OPENCODE_PORT", str(origin.server_port))

    assert llm_client._opencode_healthy() is True

    assert len(origin.seen) == 1
    assert proxy.seen == []


def test_remote_endpoint_retains_its_configured_proxy(servers, monkeypatch):
    proxy = servers()
    _proxy_environment(monkeypatch, proxy)
    url = "http://provider.invalid/models"

    assert choose_model._fetched_json(urllib.request.Request(url)) == {"healthy": True}

    assert len(proxy.seen) == 1
    assert proxy.seen[0][1] == url


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_direct_provider_request_still_succeeds(servers, monkeypatch, method):
    origin = servers()
    monkeypatch.setenv("OPENCODE_PORT", str(origin.server_port))

    assert _request(origin, method)

    assert len(origin.seen) == 1
    assert origin.seen[0][0] == method
