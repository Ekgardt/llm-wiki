"""In-process hostile LSP peer used only by protocol tests."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import unquote, urlsplit


def _frame(message: object) -> bytes:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def _read_message(stream: BinaryIO) -> dict[str, Any]:
    headers: dict[bytes, bytes] = {}
    while True:
        line = stream.readline()
        if line == b"\r\n":
            break
        name, value = line[:-2].split(b": ", 1)
        headers[name.lower()] = value
    body = stream.read(int(headers[b"content-length"]))
    value = json.loads(body)
    assert isinstance(value, dict)
    return value


class FakeLspPeer:
    """Server side of one socket-backed test connection."""

    def __init__(self, sock: socket.socket) -> None:
        self._socket = sock
        self.reader = sock.makefile("rb")
        self.writer = sock.makefile("wb")
        self._write_lock = threading.Lock()

    def read(self) -> dict[str, Any]:
        return _read_message(self.reader)

    def send(self, message: object) -> None:
        self.send_raw(_frame(message))

    def send_raw(self, value: bytes) -> None:
        with self._write_lock:
            self.writer.write(value)
            self.writer.flush()

    def close(self) -> None:
        try:
            self.writer.close()
        finally:
            try:
                self.reader.close()
            finally:
                self._socket.close()


def _send_oversized_frame(peer: FakeLspPeer, request_id: object) -> None:
    del request_id
    peer.send_raw(b"Content-Length: 8388609\r\n\r\n")


def _send_invalid_header(peer: FakeLspPeer, request_id: object) -> None:
    del request_id
    peer.send_raw(b"Content-Length 2\r\n\r\n{}")


def _send_duplicate_content_length(peer: FakeLspPeer, request_id: object) -> None:
    del request_id
    peer.send_raw(b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}")


def _send_wrong_charset(peer: FakeLspPeer, request_id: object) -> None:
    del request_id
    peer.send_raw(
        b"Content-Length: 2\r\n"
        b"Content-Type: application/vscode-jsonrpc; charset=utf-16\r\n\r\n{}"
    )


def _send_deep_json(peer: FakeLspPeer, request_id: object) -> None:
    result: object = None
    for _ in range(64):
        result = [result]
    peer.send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _send_batch_message(peer: FakeLspPeer, request_id: object) -> None:
    peer.send([{"jsonrpc": "2.0", "id": request_id, "result": None}])


def _send_duplicate_response_id(peer: FakeLspPeer, request_id: object) -> None:
    response = {"jsonrpc": "2.0", "id": request_id, "result": None}
    peer.send_raw(_frame(response) + _frame(response))


_CONNECTION_SCENARIOS = {
    "oversized-frame": _send_oversized_frame,
    "invalid-header": _send_invalid_header,
    "duplicate-content-length": _send_duplicate_content_length,
    "wrong-charset": _send_wrong_charset,
    "json-depth-65": _send_deep_json,
    "batch-message": _send_batch_message,
    "duplicate-response-id": _send_duplicate_response_id,
}


class FakeLspServer:
    """Own socket pairs and scripted peers for a protocol test."""

    def __init__(self) -> None:
        self.peers: list[FakeLspPeer] = []
        self.protocols: list[object] = []
        self.threads: list[threading.Thread] = []
        self.failures: list[BaseException] = []

    def start(
        self,
        handler: Callable[[FakeLspPeer], None] | None = None,
        *,
        generation_nonce: str = "generation-a",
        fatal_callback: Callable[[str], None] | None = None,
        warning_callback: Callable[[str], None] | None = None,
        server_request_handlers: dict[str, Callable[[object], object]] | None = None,
        server_notification_handlers: dict[str, Callable[[object], None]] | None = None,
    ):
        from lsp_protocol import LspProtocol

        client_socket, server_socket = socket.socketpair()
        client_reader = client_socket.makefile("rb")
        client_writer = client_socket.makefile("wb")
        peer = FakeLspPeer(server_socket)
        protocol = LspProtocol(
            client_reader,
            client_writer,
            generation_nonce,
            fatal_callback=fatal_callback or (lambda _reason: None),
            warning_callback=warning_callback,
            server_request_handlers=server_request_handlers,
            server_notification_handlers=server_notification_handlers,
        )
        protocol._test_socket = client_socket
        self.peers.append(peer)
        self.protocols.append(protocol)
        if handler is not None:
            thread = threading.Thread(
                target=self._run_handler,
                args=(handler, peer),
                daemon=True,
            )
            self.threads.append(thread)
            thread.start()
        return protocol

    def connection(self, scenario: str):
        sender = _CONNECTION_SCENARIOS.get(scenario)
        if sender is None:
            raise AssertionError(f"unknown scenario: {scenario}")

        def handler(peer: FakeLspPeer) -> None:
            request = peer.read()
            sender(peer, request["id"])

        return self.start(handler)

    def _run_handler(self, handler: Callable[[FakeLspPeer], None], peer: FakeLspPeer) -> None:
        try:
            handler(peer)
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        except BaseException as exc:  # pragma: no cover - surfaced by close
            self.failures.append(exc)

    @staticmethod
    def _close_protocol(protocol: object) -> None:
        protocol.close()
        client_socket = protocol._test_socket
        try:
            client_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        client_socket.close()

    def close(self) -> None:
        for protocol in self.protocols:
            self._close_protocol(protocol)
        for peer in self.peers:
            peer.close()
        for thread in self.threads:
            thread.join(timeout=1)
        if self.failures:
            raise self.failures[0]


def _semantic_local_path(uri: str) -> Path:
    parsed = urlsplit(uri)
    path = unquote(parsed.path)
    if os.name == "nt" and len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return Path(path)


def _expanded_string(value: str, replacements: dict[str, str]) -> str:
    for marker, replacement in replacements.items():
        value = value.replace(marker, replacement)
    return value


def _expanded_mapping(value: dict, replacements: dict[str, str]) -> dict:
    return {
        str(key): _semantic_expand(item, replacements)
        for key, item in value.items()
    }


def _expanded_list(value: list, replacements: dict[str, str]) -> list:
    return [_semantic_expand(item, replacements) for item in value]


_EXPANDERS = {str: _expanded_string, list: _expanded_list, dict: _expanded_mapping}


def _semantic_expand(value: object, replacements: dict[str, str]) -> object:
    """Replace every marker inside strings, lists and mappings alike."""
    expander = _EXPANDERS.get(type(value))
    if expander is None:
        return value
    return expander(value, replacements)


def _default_capabilities() -> dict[str, object]:
    return {
        "callHierarchyProvider": True,
        "definitionProvider": {"workDoneProgress": True},
        "documentSymbolProvider": {"workDoneProgress": True},
        "hoverProvider": {"workDoneProgress": True},
        "referencesProvider": {"workDoneProgress": True},
        "textDocumentSync": 2,
        "typeDefinitionProvider": {"workDoneProgress": True},
        "workspaceSymbolProvider": {"workDoneProgress": True},
    }


def _default_document_symbols() -> list[dict[str, object]]:
    return [
        {
            "name": "Service",
            "kind": 5,
            "range": {
                "start": {"line": 4, "character": 0},
                "end": {"line": 9, "character": 37},
            },
            "selectionRange": {
                "start": {"line": 4, "character": 6},
                "end": {"line": 4, "character": 13},
            },
            "children": [
                {
                    "name": "execute",
                    "kind": 6,
                    "range": {
                        "start": {"line": 8, "character": 4},
                        "end": {"line": 9, "character": 37},
                    },
                    "selectionRange": {
                        "start": {"line": 8, "character": 8},
                        "end": {"line": 8, "character": 15},
                    },
                }
            ],
        }
    ]


def _default_call_item(uri: str, name: str, line: int) -> dict[str, object]:
    return {
        "name": name,
        "kind": 12,
        "uri": uri,
        "range": {
            "start": {"line": line, "character": 0},
            "end": {"line": line, "character": 20},
        },
        "selectionRange": {
            "start": {"line": line, "character": 4},
            "end": {"line": line, "character": 11},
        },
        "data": {"fixture": name},
    }


def _semantic_default_results(service_uri: str, api_uri: str) -> dict:
    """The canned answer the fixture gives to each method it serves."""
    return {
        "textDocument/documentSymbol": _default_document_symbols(),
        "textDocument/definition": [
                {
                    "uri": api_uri,
                    "range": {
                        "start": {"line": 1, "character": 8},
                        "end": {"line": 1, "character": 14},
                    },
                }
            ],
        "textDocument/references": [
                {
                    "uri": service_uri,
                    "range": {
                        "start": {"line": 9, "character": 15},
                        "end": {"line": 9, "character": 25},
                    },
                },
                {
                    "targetUri": api_uri,
                    "targetRange": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 2, "character": 28},
                    },
                    "targetSelectionRange": {
                        "start": {"line": 1, "character": 8},
                        "end": {"line": 1, "character": 14},
                    },
                },
            ],
        "textDocument/typeDefinition": {
                "targetUri": api_uri,
                "targetRange": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 2, "character": 28},
                },
                "targetSelectionRange": {
                    "start": {"line": 0, "character": 6},
                    "end": {"line": 0, "character": 15},
                },
            },
        "textDocument/implementation": [
                {
                    "uri": service_uri,
                    "range": {
                        "start": {"line": 4, "character": 6},
                        "end": {"line": 4, "character": 13},
                    },
                }
            ],
        "textDocument/hover": {
                "contents": [
                    {"language": "python", "value": "def execute(value: str) -> str"},
                    {"kind": "plaintext", "value": "Execute the service."},
                ],
                "range": {
                    "start": {"line": 8, "character": 8},
                    "end": {"line": 8, "character": 15},
                },
            },
        "workspace/symbol": [
                {
                    "name": "Service",
                    "kind": 5,
                    "location": {
                        "uri": service_uri,
                        "range": {
                            "start": {"line": 4, "character": 6},
                            "end": {"line": 4, "character": 13},
                        },
                    },
                },
                {
                    "name": "PublicApi",
                    "kind": 5,
                    "location": {
                        "uri": api_uri,
                        "range": {
                            "start": {"line": 0, "character": 6},
                            "end": {"line": 0, "character": 15},
                        },
                    },
                },
            ],
        "textDocument/prepareCallHierarchy": [_default_call_item(service_uri, "execute", 8)],
        "callHierarchy/incomingCalls": [
                {
                    "from": _default_call_item(service_uri, "format_value", 12),
                    "fromRanges": [
                        {
                            "start": {"line": 14, "character": 11},
                            "end": {"line": 14, "character": 31},
                        }
                    ],
                }
            ],
        "callHierarchy/outgoingCalls": [
                {
                    "to": _default_call_item(api_uri, "format", 1),
                    "fromRanges": [
                        {
                            "start": {"line": 9, "character": 15},
                            "end": {"line": 9, "character": 25},
                        }
                    ],
                }
            ],
    }

def _default_diagnostics(uri: str, version: int, api_uri: str) -> list:
    return [
        {
            "uri": uri,
            "version": version,
            "diagnostics": [
                {
                    "range": {
                        "start": {"line": 9, "character": 15},
                        "end": {"line": 9, "character": 25},
                    },
                    "severity": 2,
                    "code": "reportUnknownMemberType",
                    "message": "Member type is unknown",
                    "relatedInformation": [
                        {
                            "location": {
                                "uri": api_uri,
                                "range": {
                                    "start": {"line": 1, "character": 8},
                                    "end": {"line": 1, "character": 14},
                                },
                            },
                            "message": "Declared here",
                        }
                    ],
                }
            ],
        }
    ]


def _text_document(request: dict) -> dict | None:
    params = request.get("params")
    if not isinstance(params, dict):
        return None
    text_document = params.get("textDocument")
    if not isinstance(text_document, dict):
        return None
    return text_document


def _request_uri(request: dict) -> str | None:
    """The document URI a request names, when it names one at all."""
    text_document = _text_document(request)
    if text_document is None:
        return None
    uri = text_document.get("uri")
    if not isinstance(uri, str):
        return None
    return uri


def _opened_document(request: dict) -> tuple[str, int] | None:
    params = request.get("params")
    text_document = params.get("textDocument") if isinstance(params, dict) else None
    if not isinstance(text_document, dict):
        return None
    uri = text_document.get("uri")
    version = text_document.get("version")
    if not isinstance(uri, str) or not isinstance(version, int):
        return None
    return uri, version


def _positive_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return value > 0


_SEMANTIC_STOP = "stop"
_SEMANTIC_DONE = "done"

_DEFAULT_CONFIGURATION_ITEMS = [
    {"section": "python"},
    {"section": "python.analysis"},
    {"section": "pyright"},
    {"section": "unknown"},
]

_BENIGN_SERVER_REQUESTS = (
    ("semantic-progress-create", "window/workDoneProgress/create", {"token": "semantic-progress"}),
    ("semantic-register", "client/registerCapability", {"registrations": []}),
    ("semantic-unregister", "client/unregisterCapability", {"unregisterations": []}),
)

_PROGRESS_MESSAGES = (
    {
        "jsonrpc": "2.0",
        "method": "$/progress",
        "params": {
            "token": "semantic-progress",
            "value": {"kind": "begin", "title": "Analyzing"},
        },
    },
    {"jsonrpc": "2.0", "method": "pyright/beginProgress"},
    {
        "jsonrpc": "2.0",
        "method": "pyright/reportProgress",
        "params": {"message": "Analyzing files"},
    },
    {"jsonrpc": "2.0", "method": "pyright/endProgress"},
)


class _SemanticServer:
    """The semantic fixture: one method per thing the protocol asks of it."""

    def __init__(self, reader, writer) -> None:
        self._reader = reader
        self._writer = writer
        self._write_lock = threading.Lock()
        self.config: dict[str, Any] = {}
        self.event_log: Path | None = None
        self.initialized = False
        self.documents: dict[str, int] = {}
        self.root_uri = ""
        self.root_path: Path | None = None
        self.document_symbol_failures = 0

    def send(self, message: object) -> None:
        with self._write_lock:
            self._writer.write(_frame(message))
            self._writer.flush()

    def record(self, kind: str, **values: object) -> None:
        if self.event_log is None:
            return
        event = {"kind": kind, "pid": os.getpid(), **values}
        with self.event_log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, sort_keys=True, separators=(",", ":")))
            stream.write("\n")

    def response(self, request: dict[str, Any], result: object) -> None:
        self.send({"jsonrpc": "2.0", "id": request["id"], "result": result})

    def send_error(self, request: dict[str, Any], code: int, message: str) -> None:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"code": code, "message": message},
            }
        )

    def server_request(self, request_id: str, method: str, params: object) -> object:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        reply = _read_message(self._reader)
        self.record("client-response", request_id=request_id, response=reply)
        if reply.get("id") != request_id or "result" not in reply:
            raise RuntimeError(f"semantic client did not answer {method}")
        return reply["result"]

    def load_config(self, initialize: dict[str, Any]) -> None:
        params = initialize.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("rootUri"), str):
            raise RuntimeError("semantic initialize rootUri missing")
        self.root_uri = params["rootUri"].rstrip("/")
        self.root_path = _semantic_local_path(self.root_uri)
        self._read_config_file(self.root_path / ".fake-lsp-server.json")
        log_value = self.config.get("event_log")
        if isinstance(log_value, str):
            self.event_log = Path(log_value)

    def _read_config_file(self, config_path: Path) -> None:
        if not config_path.exists():
            return
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise RuntimeError("semantic configuration must be an object")
        self.config = loaded

    def replacements(self, request_uri: str = "") -> dict[str, str]:
        assert self.root_path is not None
        return {
            "$ROOT_URI": self.root_uri,
            "$SERVICE_URI": self.root_uri + "/pkg/service.py",
            "$API_URI": self.root_uri + "/pkg/api.py",
            "$UNICODE_URI": self.root_uri + "/pkg/unicode_api.py",
            "$EXTERNAL_URI": (self.root_path.parent / "external.py").resolve().as_uri(),
            "$REQUEST_URI": request_uri,
        }

    def publish_diagnostics(self, uri: str, version: int) -> None:
        notifications = self.config.get("diagnostic_notifications")
        if not isinstance(notifications, list):
            notifications = _default_diagnostics(
                uri, version, self.root_uri + "/pkg/api.py"
            )
        expanded = _semantic_expand(notifications, self.replacements(uri))
        assert isinstance(expanded, list)
        for notification in expanded:
            self._publish_one(notification)

    def _publish_one(self, notification: object) -> None:
        if not isinstance(notification, dict):
            return
        self.send(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": notification,
            }
        )

    def serve(self) -> None:
        while True:
            request = _read_message(self._reader)
            method = str(request.get("method"))
            if method == "initialize":
                self.load_config(request)
            self.record("client-message", method=method, message=request)
            if self._dispatch(method, request) == _SEMANTIC_STOP:
                return

    def _dispatch(self, method: str, request: dict[str, Any]) -> str:
        handlers = {
            "initialize": self._on_initialize,
            "initialized": self._on_initialized,
            "workspace/didChangeConfiguration": self._on_ignored,
            "textDocument/didOpen": self._on_did_open,
            "shutdown": self._on_shutdown,
            "exit": self._on_exit,
        }
        handler = handlers.get(method)
        if handler is None:
            return self._on_request(method, request)
        return handler(request)

    def _on_initialize(self, request: dict[str, Any]) -> str:
        behavior = self.config.get("initialize_behavior")
        if behavior == "broken":
            return _SEMANTIC_STOP
        if behavior == "timeout":
            time.sleep(60)
            return _SEMANTIC_STOP
        self._exchange_configuration()
        self._benign_server_requests()
        self._push_progress()
        capabilities = self.config.get("capabilities", _default_capabilities())
        self.response(request, {"capabilities": capabilities})
        return _SEMANTIC_DONE

    def _exchange_configuration(self) -> None:
        items = self.config.get("configuration_items", _DEFAULT_CONFIGURATION_ITEMS)
        configuration = self.server_request(
            "semantic-configuration",
            "workspace/configuration",
            {"items": items},
        )
        self.record("configuration", values=configuration)

    def _benign_server_requests(self) -> None:
        if not self.config.get("benign_requests"):
            return
        for request_id, method, params in _BENIGN_SERVER_REQUESTS:
            self.server_request(request_id, method, params)

    def _push_progress(self) -> None:
        if not self.config.get("push_progress"):
            return
        for message in _PROGRESS_MESSAGES:
            self.send(message)

    def _on_initialized(self, request: dict[str, Any]) -> str:
        del request
        self.initialized = True
        return _SEMANTIC_DONE

    @staticmethod
    def _on_ignored(request: dict[str, Any]) -> str:
        del request
        return _SEMANTIC_DONE

    def _on_shutdown(self, request: dict[str, Any]) -> str:
        self.response(request, None)
        return _SEMANTIC_DONE

    @staticmethod
    def _on_exit(request: dict[str, Any]) -> str:
        del request
        return _SEMANTIC_STOP

    def _on_did_open(self, request: dict[str, Any]) -> str:
        opened = _opened_document(request)
        if opened is None:
            return _SEMANTIC_DONE
        uri, version = opened
        self._register_document(uri, version)
        self._start_diagnostics(uri, version)
        return _SEMANTIC_DONE

    def _register_document(self, uri: str, version: int) -> None:
        if uri in self.documents:
            self.record("duplicate-did-open", uri=uri, version=version)
            raise RuntimeError("duplicate textDocument/didOpen")
        self.documents[uri] = version

    def _start_diagnostics(self, uri: str, version: int) -> None:
        """A configured delay publishes on a timer; otherwise publish at once."""
        delay = self.config.get("diagnostics_delay_seconds", 0)
        if _positive_number(delay):
            timer = threading.Timer(
                delay, self.publish_diagnostics, args=(uri, version)
            )
            timer.daemon = True
            timer.start()
            return
        if self.config.get("push_diagnostics", True):
            self.publish_diagnostics(uri, version)

    def _on_request(self, method: str, request: dict[str, Any]) -> str:
        if "id" not in request:
            return _SEMANTIC_DONE
        if self._crash_once(method):
            return _SEMANTIC_STOP
        return self._answer_request(method, request)

    def _answer_request(self, method: str, request: dict[str, Any]) -> str:
        self._delay_response(method)
        request_uri = _request_uri(request)
        if not self._is_ready(method, request_uri):
            self.send_error(request, -32002, "document is not ready")
            return _SEMANTIC_DONE
        if self._document_symbols_fail(method, request, request_uri):
            return _SEMANTIC_DONE
        self.response(request, self._result_for(method, request))
        return _SEMANTIC_DONE

    def _crash_once(self, method: str) -> bool:
        """The configured method crashes the server once, and leaves its marker."""
        if method != self.config.get("crash_once_method"):
            return False
        crash_marker = self.config.get("crash_marker")
        if not isinstance(crash_marker, str):
            return False
        return _claim_marker(crash_marker)

    def _delay_response(self, method: str) -> None:
        response_delays = self.config.get("response_delays")
        if not isinstance(response_delays, dict):
            return
        delay = response_delays.get(method)
        if _positive_number(delay):
            time.sleep(delay)

    def _is_ready(self, method: str, request_uri: str | None) -> bool:
        if not self.config.get("require_initialized_open", True):
            return True
        if method == "workspace/symbol":
            return True
        return self.initialized and self._document_is_open(method, request_uri)

    def _document_is_open(self, method: str, request_uri: str | None) -> bool:
        if not method.startswith("textDocument/"):
            return True
        if request_uri is None:
            return True
        return request_uri in self.documents

    def _document_symbols_fail(
        self,
        method: str,
        request: dict[str, Any],
        request_uri: str | None,
    ) -> bool:
        """Symbols fail for a configured URI, and for a configured number of tries."""
        if method != "textDocument/documentSymbol":
            return False
        return self._symbols_not_ready(request, request_uri)

    def _symbols_not_ready(
        self,
        request: dict[str, Any],
        request_uri: str | None,
    ) -> bool:
        if self._uri_marked_failing(request_uri):
            self.send_error(request, -32003, "symbols not ready")
            return True
        if not self._failure_budget_left():
            return False
        self.document_symbol_failures += 1
        self.send_error(request, -32003, "symbols not ready")
        return True

    def _uri_marked_failing(self, request_uri: str | None) -> bool:
        failure_uris = self.config.get("document_symbol_failure_uris", [])
        expanded = _semantic_expand(
            failure_uris, self.replacements(request_uri or "")
        )
        if not isinstance(expanded, list):
            return False
        return request_uri in expanded

    def _failure_budget_left(self) -> bool:
        failures = self.config.get("document_symbol_failures", 0)
        if not isinstance(failures, int):
            return False
        return self.document_symbol_failures < failures

    def _result_for(self, method: str, request: dict[str, Any]) -> object:
        configured = self.config.get("responses")
        if not isinstance(configured, dict) or method not in configured:
            return self._default_result(request)
        return _semantic_expand(
            configured[method], self.replacements(_request_uri(request) or "")
        )

    def _default_result(self, request: dict[str, Any]) -> object:
        values = self.replacements(_request_uri(request) or "")
        answers = _semantic_default_results(
            values["$SERVICE_URI"], values["$API_URI"]
        )
        return answers.get(str(request.get("method")))


def _run_semantic_server(args: argparse.Namespace) -> None:
    del args
    _SemanticServer(sys.stdin.buffer, sys.stdout.buffer).serve()


def _process_server_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stderr-bytes", type=int, default=0)
    parser.add_argument("--stderr-linger-seconds", type=float, default=3.0)
    parser.add_argument("--report-environment", action="store_true")
    parser.add_argument("--echo", action="store_true")
    parser.add_argument("--exit-while-pending", action="store_true")
    parser.add_argument("--ignored-secret")
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--spawn-descendant", action="store_true")
    parser.add_argument("--spawn-setsid-descendant", action="store_true")
    parser.add_argument("--descendant-exit-after", type=float)
    parser.add_argument("--exit-after-descendant-spawn", action="store_true")
    parser.add_argument("--descendant-pid-file")
    parser.add_argument("--descendant-pid-log")
    parser.add_argument("--lifecycle", action="store_true")
    parser.add_argument("--ignore-shutdown", action="store_true")
    parser.add_argument("--event-log")
    parser.add_argument("--crash-once-marker")
    parser.add_argument("--always-crash", action="store_true")
    parser.add_argument("--application-error", action="store_true")
    parser.add_argument("--hang-once-marker")
    parser.add_argument("--idle-exit-marker")
    parser.add_argument("--idle-exit-gate-prefix")
    parser.add_argument("--hang-then-exit", action="store_true")
    parser.add_argument("--bootstrap-handshake", action="store_true")
    parser.add_argument(
        "--startup-callback",
        choices=("request", "notification"),
    )
    parser.add_argument("--startup-callback-marker")
    parser.add_argument("--query-crash-once-marker")
    parser.add_argument("--require-initialized-query", action="store_true")
    parser.add_argument("--stdio", action="store_true")
    parser.add_argument("--cancellationReceive")
    return parser


def _report_group_signal(_signum: int, _frame: object) -> None:
    print(json.dumps({"group_signal": "SIGTERM"}), flush=True)


def _terminate_descendant(descendant: subprocess.Popen) -> None:
    descendant.terminate()
    try:
        descendant.wait(timeout=2)
    except subprocess.TimeoutExpired:
        descendant.kill()
        descendant.wait(timeout=2)


def _run_setsid_descendant_fixture() -> None:
    """A descendant that leaves the process group, and is reaped by hand."""
    if os.name != "posix":
        raise RuntimeError("setsid descendant fixture requires POSIX")
    signal.signal(signal.SIGTERM, _report_group_signal)
    descendant = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,time; os.setsid(); print('ready', flush=True); time.sleep(60)",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    assert descendant.stdout is not None
    assert descendant.stdout.readline() == b"ready\n"
    print(json.dumps({"descendant_pid": descendant.pid}), flush=True)
    try:
        sys.stdin.buffer.readline()
    finally:
        _terminate_descendant(descendant)
    print(json.dumps({"descendant_reaped": True}), flush=True)


def _descendant_code(args: argparse.Namespace) -> str:
    if args.descendant_exit_after is None:
        return "import time; time.sleep(60)"
    return (
        f"import os,time; time.sleep({args.descendant_exit_after!r}); os._exit(17)"
    )


def _write_pid_file(path: str, pid: int) -> None:
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="ascii") as stream:
        stream.write(str(pid))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _report_descendant_pid(args: argparse.Namespace, pid: int) -> None:
    """The pid goes to a file, to a log, or — with neither asked for — to stdout."""
    if args.descendant_pid_file:
        _write_pid_file(args.descendant_pid_file, pid)
    elif not args.descendant_pid_log:
        print(json.dumps({"descendant_pid": pid}), flush=True)
    if args.descendant_pid_log:
        with open(args.descendant_pid_log, "a", encoding="ascii") as stream:
            stream.write(f"{pid}\n")


def _spawn_descendant_and_exit(args: argparse.Namespace) -> bool:
    """Spawn the descendant, and say whether the fixture ends right there."""
    if not args.spawn_descendant:
        return False
    descendant = subprocess.Popen(
        [sys.executable, "-c", _descendant_code(args)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    _report_descendant_pid(args, descendant.pid)
    return bool(args.exit_after_descendant_spawn)


def _write_message(message: object) -> None:
    sys.stdout.buffer.write(_frame(message))
    sys.stdout.buffer.flush()


def _startup_callback_due(args: argparse.Namespace) -> bool:
    if not args.startup_callback:
        return False
    if args.startup_callback_marker is None:
        return True
    return os.path.exists(args.startup_callback_marker)


def _startup_message(kind: str) -> dict[str, object]:
    method = "workspace/configuration" if kind == "request" else "$/progress"
    message: dict[str, object] = {
        "jsonrpc": "2.0",
        "method": method,
        "params": {"startup_callback": True},
    }
    if kind == "request":
        message["id"] = "startup-callback"
    return message


def _require_startup_reply(kind: str) -> None:
    if kind != "request":
        return
    reply = _read_message(sys.stdin.buffer)
    if reply.get("id") != "startup-callback" or reply.get("result") is not True:
        raise RuntimeError("startup callback request failed")


def _startup_callback(args: argparse.Namespace) -> None:
    if not _startup_callback_due(args):
        return
    _write_message(_startup_message(args.startup_callback))
    _require_startup_reply(args.startup_callback)


def _await_gate(gate: Path) -> None:
    deadline = time.monotonic() + 10
    while not gate.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("idle exit gate was not released")
        time.sleep(0.01)


def _idle_wait(args: argparse.Namespace, first: bool) -> None:
    if not args.idle_exit_gate_prefix:
        time.sleep(0.05 if first else 0.75)
        return
    suffix = "first" if first else "second"
    _await_gate(Path(f"{args.idle_exit_gate_prefix}.{suffix}"))


def _idle_exit(args: argparse.Namespace) -> bool:
    """The idle fixture waits once, marks itself, and exits without serving."""
    if not args.idle_exit_marker:
        return False
    first = not os.path.exists(args.idle_exit_marker)
    if first:
        with open(args.idle_exit_marker, "wb"):
            pass
    _idle_wait(args, first)
    return True


def _claim_marker(marker: str | None) -> bool:
    """True exactly once: the first call creates the marker, later calls see it."""
    if not marker:
        return False
    if os.path.exists(marker):
        return False
    with open(marker, "wb"):
        pass
    return True


def _write_bootstrap_requests() -> None:
    sys.stdout.buffer.write(
        _frame(
            {
                "jsonrpc": "2.0",
                "id": "bootstrap-configuration",
                "method": "workspace/configuration",
                "params": {"items": [{"section": "python"}]},
            }
        )
    )
    sys.stdout.buffer.write(
        _frame(
            {
                "jsonrpc": "2.0",
                "method": "$/progress",
                "params": {"token": "bootstrap", "value": {"kind": "begin"}},
            }
        )
    )
    sys.stdout.buffer.flush()


def _write_bootstrap_reply(request: dict, configured: bool) -> None:
    if not configured:
        _write_message(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"code": -32002, "message": "configuration required"},
            }
        )
        return
    _write_message(
        {"jsonrpc": "2.0", "id": request["id"], "result": {"capabilities": {}}}
    )


def _query_response(request: dict, initialized: bool) -> dict[str, object]:
    if not initialized:
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {"code": -32002, "message": "server not initialized"},
        }
    return {
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {"initialized": True, "pid": os.getpid()},
    }


def _generic_response(request: dict, application_error: bool) -> dict[str, object]:
    if application_error:
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {"code": -32001, "message": "application failed"},
        }
    return {
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": request.get("params"),
    }


class _LifecycleServer:
    """The process fixture's request loop: one method per behaviour a flag asks for."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.initialized = False

    def serve(self) -> None:
        while True:
            request = _read_message(sys.stdin.buffer)
            if self._handle(request) == _SEMANTIC_STOP:
                return

    def _handle(self, request: dict) -> str:
        if self.args.hang_then_exit:
            time.sleep(0.1)
            return _SEMANTIC_STOP
        method = str(request.get("method"))
        self._log_method(method)
        interruption = self._interruption(method)
        if interruption is not None:
            return interruption
        return self._answer(method, request)

    def _log_method(self, method: str) -> None:
        if not self.args.event_log:
            return
        with open(self.args.event_log, "a", encoding="utf-8") as stream:
            stream.write(method + "\n")

    def _interruption(self, method: str) -> str | None:
        """Crashing and hanging, each once, driven by a marker file."""
        if self.args.always_crash:
            return _SEMANTIC_STOP
        marked = self._marker_interruption()
        if marked is not None:
            return marked
        return self._query_crash(method)

    def _marker_interruption(self) -> str | None:
        if _claim_marker(self.args.crash_once_marker):
            return _SEMANTIC_STOP
        if _claim_marker(self.args.hang_once_marker):
            return _SEMANTIC_DONE
        return None

    def _query_crash(self, method: str) -> str | None:
        if method != "initialized/query":
            return None
        if not _claim_marker(self.args.query_crash_once_marker):
            return None
        return _SEMANTIC_STOP

    def _answer(self, method: str, request: dict) -> str:
        handlers = {
            "initialize": self._on_initialize,
            "initialized": self._on_initialized,
            "shutdown": self._on_shutdown,
            "exit": self._on_exit,
            "initialized/query": self._on_query,
        }
        return handlers.get(method, self._on_other)(request)

    def _on_initialize(self, request: dict) -> str:
        if not self.args.bootstrap_handshake:
            return self._on_other(request)
        _write_bootstrap_requests()
        configuration = _read_message(sys.stdin.buffer)
        _write_bootstrap_reply(request, configuration.get("result") is True)
        return _SEMANTIC_DONE

    def _on_initialized(self, request: dict) -> str:
        del request
        self.initialized = True
        return _SEMANTIC_DONE

    def _on_shutdown(self, request: dict) -> str:
        if self.args.ignore_shutdown:
            return _SEMANTIC_DONE
        _write_message({"jsonrpc": "2.0", "id": request["id"], "result": None})
        return _SEMANTIC_DONE

    @staticmethod
    def _on_exit(request: dict) -> str:
        del request
        return _SEMANTIC_STOP

    def _on_query(self, request: dict) -> str:
        if not self.args.require_initialized_query:
            return self._on_other(request)
        _write_message(_query_response(request, self.initialized))
        return _SEMANTIC_DONE

    def _on_other(self, request: dict) -> str:
        if "id" not in request:
            return _SEMANTIC_DONE
        _write_message(_generic_response(request, self.args.application_error))
        return _SEMANTIC_DONE


def _run_lifecycle_fixture(args: argparse.Namespace) -> None:
    _startup_callback(args)
    if _idle_exit(args):
        return
    _LifecycleServer(args).serve()


def _write_stderr_bytes(args: argparse.Namespace) -> None:
    remaining = args.stderr_bytes
    offset = 0
    while remaining:
        size = min(65_537, remaining)
        chunk = bytes((offset + index) % 251 for index in range(size))
        os.write(2, chunk)
        offset += size
        remaining -= size
    if args.stderr_bytes:
        # Stay alive while the parent finishes starting this generation. On a
        # hosted Windows runner the write finished first and the exit turned
        # into `LSP process exited during generation startup`.
        time.sleep(args.stderr_linger_seconds)


def _wants_single_request(args: argparse.Namespace) -> bool:
    return bool(args.report_environment or args.echo or args.exit_while_pending)


def _single_request_result(args: argparse.Namespace, request: dict) -> object:
    if args.report_environment:
        return dict(os.environ)
    return request.get("params")


def _answer_single_request(args: argparse.Namespace) -> None:
    if not _wants_single_request(args):
        return
    request = _read_message(sys.stdin.buffer)
    if args.exit_while_pending:
        _read_message(sys.stdin.buffer)
        return
    result = _single_request_result(args, request)
    _write_message({"jsonrpc": "2.0", "id": request["id"], "result": result})


def _run_stderr_fixture(args: argparse.Namespace) -> None:
    if args.sleep_seconds:
        time.sleep(args.sleep_seconds)
        return
    _write_stderr_bytes(args)
    _answer_single_request(args)


def _run_early_fixture(args: argparse.Namespace) -> bool:
    """The fixtures that finish the process before any lifecycle loop."""
    if args.stdio:
        _run_semantic_server(args)
        return True
    if args.spawn_setsid_descendant:
        _run_setsid_descendant_fixture()
        return True
    return _spawn_descendant_and_exit(args)


def _run_process_server() -> None:
    """Each flag picks one fixture; the plain stderr/echo server is the fallback."""
    args = _process_server_parser().parse_args()
    if _run_early_fixture(args):
        return
    if args.lifecycle:
        _run_lifecycle_fixture(args)
        return
    _run_stderr_fixture(args)


if __name__ == "__main__":
    _run_process_server()
