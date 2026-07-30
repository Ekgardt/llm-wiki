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
        def handler(peer: FakeLspPeer) -> None:
            request = peer.read()
            request_id = request["id"]
            if scenario == "oversized-frame":
                peer.send_raw(b"Content-Length: 8388609\r\n\r\n")
            elif scenario == "invalid-header":
                peer.send_raw(b"Content-Length 2\r\n\r\n{}")
            elif scenario == "duplicate-content-length":
                peer.send_raw(b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}")
            elif scenario == "wrong-charset":
                peer.send_raw(
                    b"Content-Length: 2\r\n"
                    b"Content-Type: application/vscode-jsonrpc; charset=utf-16\r\n\r\n{}"
                )
            elif scenario == "json-depth-65":
                result: object = None
                for _ in range(64):
                    result = [result]
                peer.send({"jsonrpc": "2.0", "id": request_id, "result": result})
            elif scenario == "batch-message":
                peer.send([{"jsonrpc": "2.0", "id": request_id, "result": None}])
            elif scenario == "duplicate-response-id":
                response = {"jsonrpc": "2.0", "id": request_id, "result": None}
                peer.send_raw(_frame(response) + _frame(response))
            else:
                raise AssertionError(f"unknown scenario: {scenario}")

        return self.start(handler)

    def _run_handler(self, handler: Callable[[FakeLspPeer], None], peer: FakeLspPeer) -> None:
        try:
            handler(peer)
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        except BaseException as exc:  # pragma: no cover - surfaced by close
            self.failures.append(exc)

    def close(self) -> None:
        for protocol in self.protocols:
            protocol.close()
            client_socket = protocol._test_socket
            try:
                client_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client_socket.close()
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


def _semantic_expand(value: object, replacements: dict[str, str]) -> object:
    if isinstance(value, str):
        for marker, replacement in replacements.items():
            value = value.replace(marker, replacement)
        return value
    if isinstance(value, list):
        return [_semantic_expand(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _semantic_expand(item, replacements)
            for key, item in value.items()
        }
    return value


def _run_semantic_server(args: argparse.Namespace) -> None:
    reader = sys.stdin.buffer
    writer = sys.stdout.buffer
    write_lock = threading.Lock()
    config: dict[str, Any] = {}
    event_log: Path | None = None
    initialized = False
    documents: dict[str, int] = {}
    root_uri = ""
    root_path: Path | None = None
    document_symbol_failures = 0

    def send(message: object) -> None:
        with write_lock:
            writer.write(_frame(message))
            writer.flush()

    def record(kind: str, **values: object) -> None:
        if event_log is None:
            return
        event = {"kind": kind, "pid": os.getpid(), **values}
        with event_log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, sort_keys=True, separators=(",", ":")))
            stream.write("\n")

    def load_config(initialize: dict[str, Any]) -> None:
        nonlocal config, event_log, root_uri, root_path
        params = initialize.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("rootUri"), str):
            raise RuntimeError("semantic initialize rootUri missing")
        root_uri = params["rootUri"].rstrip("/")
        root_path = _semantic_local_path(root_uri)
        config_path = root_path / ".fake-lsp-server.json"
        if config_path.exists():
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise RuntimeError("semantic configuration must be an object")
            config = loaded
        log_value = config.get("event_log")
        if isinstance(log_value, str):
            event_log = Path(log_value)

    def replacements(request_uri: str = "") -> dict[str, str]:
        assert root_path is not None
        return {
            "$ROOT_URI": root_uri,
            "$SERVICE_URI": root_uri + "/pkg/service.py",
            "$API_URI": root_uri + "/pkg/api.py",
            "$UNICODE_URI": root_uri + "/pkg/unicode_api.py",
            "$EXTERNAL_URI": (root_path.parent / "external.py").resolve().as_uri(),
            "$REQUEST_URI": request_uri,
        }

    def response(request: dict[str, Any], result: object) -> None:
        send({"jsonrpc": "2.0", "id": request["id"], "result": result})

    def server_request(request_id: str, method: str, params: object) -> object:
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        reply = _read_message(reader)
        record("client-response", request_id=request_id, response=reply)
        if reply.get("id") != request_id or "result" not in reply:
            raise RuntimeError(f"semantic client did not answer {method}")
        return reply["result"]

    def default_capabilities() -> dict[str, object]:
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

    def default_document_symbols() -> list[dict[str, object]]:
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

    def default_call_item(uri: str, name: str, line: int) -> dict[str, object]:
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

    def default_result(method: str, request: dict[str, Any]) -> object:
        params = request.get("params")
        request_uri = ""
        if isinstance(params, dict):
            text_document = params.get("textDocument")
            if isinstance(text_document, dict) and isinstance(text_document.get("uri"), str):
                request_uri = text_document["uri"]
        values = replacements(request_uri)
        service_uri = values["$SERVICE_URI"]
        api_uri = values["$API_URI"]
        if method == "textDocument/documentSymbol":
            return default_document_symbols()
        if method == "textDocument/definition":
            return [
                {
                    "uri": api_uri,
                    "range": {
                        "start": {"line": 1, "character": 8},
                        "end": {"line": 1, "character": 14},
                    },
                }
            ]
        if method == "textDocument/references":
            return [
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
            ]
        if method == "textDocument/typeDefinition":
            return {
                "targetUri": api_uri,
                "targetRange": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 2, "character": 28},
                },
                "targetSelectionRange": {
                    "start": {"line": 0, "character": 6},
                    "end": {"line": 0, "character": 15},
                },
            }
        if method == "textDocument/implementation":
            return [
                {
                    "uri": service_uri,
                    "range": {
                        "start": {"line": 4, "character": 6},
                        "end": {"line": 4, "character": 13},
                    },
                }
            ]
        if method == "textDocument/hover":
            return {
                "contents": [
                    {"language": "python", "value": "def execute(value: str) -> str"},
                    {"kind": "plaintext", "value": "Execute the service."},
                ],
                "range": {
                    "start": {"line": 8, "character": 8},
                    "end": {"line": 8, "character": 15},
                },
            }
        if method == "workspace/symbol":
            return [
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
            ]
        if method == "textDocument/prepareCallHierarchy":
            return [default_call_item(service_uri, "execute", 8)]
        if method == "callHierarchy/incomingCalls":
            return [
                {
                    "from": default_call_item(service_uri, "format_value", 12),
                    "fromRanges": [
                        {
                            "start": {"line": 14, "character": 11},
                            "end": {"line": 14, "character": 31},
                        }
                    ],
                }
            ]
        if method == "callHierarchy/outgoingCalls":
            return [
                {
                    "to": default_call_item(api_uri, "format", 1),
                    "fromRanges": [
                        {
                            "start": {"line": 9, "character": 15},
                            "end": {"line": 9, "character": 25},
                        }
                    ],
                }
            ]
        return None

    def publish_diagnostics(uri: str, version: int) -> None:
        notifications = config.get("diagnostic_notifications")
        if not isinstance(notifications, list):
            notifications = [
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
                                        "uri": root_uri + "/pkg/api.py",
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
        expanded = _semantic_expand(notifications, replacements(uri))
        assert isinstance(expanded, list)
        for notification in expanded:
            if not isinstance(notification, dict):
                continue
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/publishDiagnostics",
                    "params": notification,
                }
            )

    while True:
        request = _read_message(reader)
        method = request.get("method")
        if method == "initialize":
            load_config(request)
        record("client-message", method=method, message=request)

        if method == "initialize":
            behavior = config.get("initialize_behavior")
            if behavior == "broken":
                return
            if behavior == "timeout":
                time.sleep(60)
                return
            items = config.get(
                "configuration_items",
                [
                    {"section": "python"},
                    {"section": "python.analysis"},
                    {"section": "pyright"},
                    {"section": "unknown"},
                ],
            )
            configuration = server_request(
                "semantic-configuration",
                "workspace/configuration",
                {"items": items},
            )
            record("configuration", values=configuration)
            if config.get("benign_requests"):
                server_request(
                    "semantic-progress-create",
                    "window/workDoneProgress/create",
                    {"token": "semantic-progress"},
                )
                server_request(
                    "semantic-register",
                    "client/registerCapability",
                    {"registrations": []},
                )
                server_request(
                    "semantic-unregister",
                    "client/unregisterCapability",
                    {"unregisterations": []},
                )
            if config.get("push_progress"):
                send(
                    {
                        "jsonrpc": "2.0",
                        "method": "$/progress",
                        "params": {
                            "token": "semantic-progress",
                            "value": {"kind": "begin", "title": "Analyzing"},
                        },
                    }
                )
                send({"jsonrpc": "2.0", "method": "pyright/beginProgress"})
                send(
                    {
                        "jsonrpc": "2.0",
                        "method": "pyright/reportProgress",
                        "params": {"message": "Analyzing files"},
                    }
                )
                send({"jsonrpc": "2.0", "method": "pyright/endProgress"})
            capabilities = config.get("capabilities", default_capabilities())
            response(request, {"capabilities": capabilities})
            continue

        if method == "initialized":
            initialized = True
            continue

        if method == "workspace/didChangeConfiguration":
            continue

        if method == "textDocument/didOpen":
            params = request.get("params")
            text_document = params.get("textDocument") if isinstance(params, dict) else None
            if isinstance(text_document, dict):
                uri = text_document.get("uri")
                version = text_document.get("version")
                if isinstance(uri, str) and isinstance(version, int):
                    documents[uri] = version
                    delay = config.get("diagnostics_delay_seconds", 0)
                    if isinstance(delay, (int, float)) and delay > 0:
                        timer = threading.Timer(delay, publish_diagnostics, args=(uri, version))
                        timer.daemon = True
                        timer.start()
                    elif config.get("push_diagnostics", True):
                        publish_diagnostics(uri, version)
            continue

        if method == "shutdown":
            response(request, None)
            continue
        if method == "exit":
            return
        if "id" not in request:
            continue

        crash_method = config.get("crash_once_method")
        crash_marker = config.get("crash_marker")
        if method == crash_method and isinstance(crash_marker, str):
            marker = Path(crash_marker)
            if not marker.exists():
                marker.write_bytes(b"crashed\n")
                return

        response_delays = config.get("response_delays")
        if isinstance(response_delays, dict):
            delay = response_delays.get(method)
            if isinstance(delay, (int, float)) and not isinstance(delay, bool) and delay > 0:
                time.sleep(delay)

        params = request.get("params")
        request_uri = None
        if isinstance(params, dict):
            text_document = params.get("textDocument")
            if isinstance(text_document, dict):
                request_uri = text_document.get("uri")
        if config.get("require_initialized_open", True) and method not in {
            "workspace/symbol",
        }:
            if not initialized or (
                method.startswith("textDocument/")
                and isinstance(request_uri, str)
                and request_uri not in documents
            ):
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "error": {"code": -32002, "message": "document is not ready"},
                    }
                )
                continue

        if method == "textDocument/documentSymbol":
            failure_uris = config.get("document_symbol_failure_uris", [])
            expanded_failure_uris = _semantic_expand(
                failure_uris,
                replacements(request_uri or ""),
            )
            if (
                isinstance(expanded_failure_uris, list)
                and request_uri in expanded_failure_uris
            ):
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "error": {"code": -32003, "message": "symbols not ready"},
                    }
                )
                continue
            failures = config.get("document_symbol_failures", 0)
            if isinstance(failures, int) and document_symbol_failures < failures:
                document_symbol_failures += 1
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "error": {"code": -32003, "message": "symbols not ready"},
                    }
                )
                continue

        configured_responses = config.get("responses")
        if isinstance(configured_responses, dict) and method in configured_responses:
            params = request.get("params")
            request_uri = ""
            if isinstance(params, dict):
                text_document = params.get("textDocument")
                if isinstance(text_document, dict) and isinstance(text_document.get("uri"), str):
                    request_uri = text_document["uri"]
            result = _semantic_expand(
                configured_responses[method],
                replacements(request_uri),
            )
        else:
            result = default_result(str(method), request)
        response(request, result)


def _run_process_server() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stderr-bytes", type=int, default=0)
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
    args = parser.parse_args()

    if args.stdio:
        _run_semantic_server(args)
        return

    if args.spawn_setsid_descendant:
        if os.name != "posix":
            raise RuntimeError("setsid descendant fixture requires POSIX")

        def report_group_signal(_signum: int, _frame: object) -> None:
            print(json.dumps({"group_signal": "SIGTERM"}), flush=True)

        signal.signal(signal.SIGTERM, report_group_signal)
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
            descendant.terminate()
            try:
                descendant.wait(timeout=2)
            except subprocess.TimeoutExpired:
                descendant.kill()
                descendant.wait(timeout=2)
        print(json.dumps({"descendant_reaped": True}), flush=True)
        return

    if args.spawn_descendant:
        descendant_code = (
            f"import os,time; time.sleep({args.descendant_exit_after!r}); os._exit(17)"
            if args.descendant_exit_after is not None
            else "import time; time.sleep(60)"
        )
        descendant = subprocess.Popen(
            [sys.executable, "-c", descendant_code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        if args.descendant_pid_file:
            temporary_pid_file = args.descendant_pid_file + ".tmp"
            with open(temporary_pid_file, "w", encoding="ascii") as stream:
                stream.write(str(descendant.pid))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_pid_file, args.descendant_pid_file)
        elif not args.descendant_pid_log:
            print(json.dumps({"descendant_pid": descendant.pid}), flush=True)
        if args.descendant_pid_log:
            with open(args.descendant_pid_log, "a", encoding="ascii") as stream:
                stream.write(f"{descendant.pid}\n")
        if args.exit_after_descendant_spawn:
            return

    if args.lifecycle:
        initialized = False
        if args.startup_callback and (
            args.startup_callback_marker is None
            or os.path.exists(args.startup_callback_marker)
        ):
            startup_message: dict[str, object] = {
                "jsonrpc": "2.0",
                "method": (
                    "workspace/configuration"
                    if args.startup_callback == "request"
                    else "$/progress"
                ),
                "params": {"startup_callback": True},
            }
            if args.startup_callback == "request":
                startup_message["id"] = "startup-callback"
            sys.stdout.buffer.write(_frame(startup_message))
            sys.stdout.buffer.flush()
            if args.startup_callback == "request":
                startup_response = _read_message(sys.stdin.buffer)
                if (
                    startup_response.get("id") != "startup-callback"
                    or startup_response.get("result") is not True
                ):
                    raise RuntimeError("startup callback request failed")
        if args.idle_exit_marker:
            first = not os.path.exists(args.idle_exit_marker)
            if first:
                with open(args.idle_exit_marker, "wb"):
                    pass
            time.sleep(0.05 if first else 0.75)
            return
        while True:
            request = _read_message(sys.stdin.buffer)
            if args.hang_then_exit:
                time.sleep(0.1)
                return
            method = request.get("method")
            if args.event_log:
                with open(args.event_log, "a", encoding="utf-8") as stream:
                    stream.write(str(method) + "\n")
            if args.always_crash:
                return
            if args.crash_once_marker and not os.path.exists(args.crash_once_marker):
                with open(args.crash_once_marker, "wb"):
                    pass
                return
            if args.hang_once_marker and not os.path.exists(args.hang_once_marker):
                with open(args.hang_once_marker, "wb"):
                    pass
                continue
            if (
                method == "initialized/query"
                and args.query_crash_once_marker
                and not os.path.exists(args.query_crash_once_marker)
            ):
                with open(args.query_crash_once_marker, "wb"):
                    pass
                return
            if method == "initialize" and args.bootstrap_handshake:
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
                configuration = _read_message(sys.stdin.buffer)
                if configuration.get("result") is not True:
                    sys.stdout.buffer.write(
                        _frame(
                            {
                                "jsonrpc": "2.0",
                                "id": request["id"],
                                "error": {
                                    "code": -32002,
                                    "message": "configuration required",
                                },
                            }
                        )
                    )
                else:
                    sys.stdout.buffer.write(
                        _frame(
                            {
                                "jsonrpc": "2.0",
                                "id": request["id"],
                                "result": {"capabilities": {}},
                            }
                        )
                    )
                sys.stdout.buffer.flush()
                continue
            if method == "initialized":
                initialized = True
                continue
            if method == "shutdown":
                if args.ignore_shutdown:
                    continue
                sys.stdout.buffer.write(
                    _frame({"jsonrpc": "2.0", "id": request["id"], "result": None})
                )
                sys.stdout.buffer.flush()
            elif method == "exit":
                return
            elif method == "initialized/query" and args.require_initialized_query:
                if initialized:
                    response = {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {"initialized": True, "pid": os.getpid()},
                    }
                else:
                    response = {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "error": {"code": -32002, "message": "server not initialized"},
                    }
                sys.stdout.buffer.write(_frame(response))
                sys.stdout.buffer.flush()
            elif args.application_error and "id" in request:
                sys.stdout.buffer.write(
                    _frame(
                        {
                            "jsonrpc": "2.0",
                            "id": request["id"],
                            "error": {"code": -32001, "message": "application failed"},
                        }
                    )
                )
                sys.stdout.buffer.flush()
            elif "id" in request:
                sys.stdout.buffer.write(
                    _frame(
                        {
                            "jsonrpc": "2.0",
                            "id": request["id"],
                            "result": request.get("params"),
                        }
                    )
                )
                sys.stdout.buffer.flush()

    if args.sleep_seconds:
        time.sleep(args.sleep_seconds)
        return

    remaining = args.stderr_bytes
    offset = 0
    while remaining:
        size = min(65_537, remaining)
        chunk = bytes((offset + index) % 251 for index in range(size))
        os.write(2, chunk)
        offset += size
        remaining -= size

    if args.report_environment or args.echo or args.exit_while_pending:
        request = _read_message(sys.stdin.buffer)
        if args.exit_while_pending:
            _read_message(sys.stdin.buffer)
            return
        result: object = dict(os.environ) if args.report_environment else request.get("params")
        sys.stdout.buffer.write(
            _frame({"jsonrpc": "2.0", "id": request["id"], "result": result})
        )
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    _run_process_server()
