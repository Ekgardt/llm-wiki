"""The stdio memory server's supervisor: the server runs in a child it can reload.

A Claude Code session keeps its MCP server for its whole life, and the nightly
fast-forward changes the code under it. On 2026-09-23 a server started at 10:41
kept the old `bounded_io` in memory, imported the new `evidence_graph` lazily at
11:07, and answered every `get_architecture` with `operation_failed`.

This process owns the connection to the client and runs the real server
(`mcp_server.py` with `LLM_WIKI_MCP_WORKER=1`) as a child. It forwards
newline-delimited JSON-RPC in both directions, remembers the client's
`initialize` request and `notifications/initialized`, and tracks the requests in
flight. When a request arrives, nothing is in flight and the code under
`scripts/` has changed, it stops the child, starts a new one, replays the
initialisation under a private id whose answer it swallows, and forwards the
request. A child that exits on its own has every request in flight answered with
an error, and the next request starts a new one.

It imports only the standard library, so its own code is the one thing a
reload cannot change. See
`docs/research/2026-09-24-the-memory-server-reloads-its-own-code.md`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

WORKER_ENV = "LLM_WIKI_MCP_WORKER"
INIT_ID_PREFIX = "llm-wiki-supervisor:initialize:"
STOP_SECONDS = 5.0
# After its input closes the worker waits up to 30 s for model inference
# (`mcp_server.SHUTDOWN_INFERENCE_SECONDS`); a signal before that lands mid-settle.
GRACEFUL_STOP_SECONDS = 35.0
EXIT_ERROR_CODE = -32603
SCRIPTS = Path(__file__).resolve().parent
FINGERPRINT_SUFFIXES = (".py", ".json")


def code_fingerprint(scripts: Path) -> str:
    """Every server source file by path, size and modification time."""
    digest = hashlib.sha256()
    for path in sorted(_source_files(scripts)):
        digest.update(f"{path.relative_to(scripts)}\0{_file_identity(path)}\n".encode())
    return digest.hexdigest()


def _file_identity(path: Path) -> str:
    """Size and time, or `gone` for a file removed after it was listed (B-21)."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return "gone"
    return f"{stat.st_size}\0{stat.st_mtime_ns}"


def _source_files(scripts: Path) -> list[Path]:
    return [
        path
        for path in scripts.rglob("*")
        if path.suffix in FINGERPRINT_SUFFIXES and "__pycache__" not in path.parts
    ]


def decoded(line: bytes) -> dict | None:
    """The JSON-RPC message on one line, or None for anything else."""
    try:
        value = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def is_request(message: dict | None) -> bool:
    return bool(message) and "method" in message and "id" in message


def is_response(message: dict | None) -> bool:
    return bool(message) and "id" in message and "method" not in message


def _id_key(identifier: object) -> str:
    return json.dumps(identifier, sort_keys=True)


def error_line(identifier: object, message: str) -> bytes:
    body = {"jsonrpc": "2.0", "id": identifier, "error": {"code": EXIT_ERROR_CODE, "message": message}}
    return json.dumps(body).encode("utf-8") + b"\n"


@dataclass
class Child:
    process: subprocess.Popen
    generation: int
    fingerprint: str


@dataclass
class Session:
    """What the supervisor must remember to stand in for the client."""

    initialize: dict | None = None
    initialized: dict | None = None
    in_flight: dict[str, object] = field(default_factory=dict)
    child: Child | None = None
    generation: int = 0


class Supervisor:
    def __init__(
        self,
        command: list[str],
        scripts: Path,
        output: BinaryIO,
        fingerprint: Callable[[Path], str] = code_fingerprint,
    ) -> None:
        self._command = command
        self._scripts = scripts
        self._output = output
        self._fingerprint = fingerprint
        self._events: queue.Queue = queue.Queue()
        self.session = Session()

    # --- events ---------------------------------------------------------

    def feed_client(self, stream: BinaryIO) -> None:
        """Read the client's lines on a thread; end of input ends the session."""
        threading.Thread(target=self._read_client, args=(stream,), daemon=True).start()

    def _read_client(self, stream: BinaryIO) -> None:
        for line in iter(stream.readline, b""):
            self._events.put(("client", line))
        self._events.put(("client_eof", None))

    def _read_child(self, child: Child) -> None:
        for line in iter(child.process.stdout.readline, b""):
            self._events.put(("child", (child.generation, line)))
        self._events.put(("child_exit", child.generation))

    def run(self) -> int:
        handlers = {
            "client": self._on_client,
            "client_eof": self._on_client_eof,
            "child": self._on_child,
            "child_exit": self._on_child_exit,
        }
        while True:
            kind, payload = self._events.get()
            if handlers[kind](payload) is False:
                return 0

    # --- client to child ------------------------------------------------

    def _on_client(self, line: bytes) -> None:
        message = decoded(line)
        self._remember(message)
        if is_request(message):
            self._before_request(message)
        self._ensure_child(message)
        self._write_child(line)

    def _remember(self, message: dict | None) -> None:
        method = (message or {}).get("method")
        remember = {
            "initialize": self._remember_initialize,
            "notifications/initialized": self._remember_initialized,
            "notifications/cancelled": self._forget_cancelled,
        }.get(method)
        if remember is not None:
            remember(message)

    def _remember_initialize(self, message: dict) -> None:
        self.session.initialize = message

    def _remember_initialized(self, message: dict) -> None:
        self.session.initialized = message

    def _forget_cancelled(self, message: dict) -> None:
        """A cancelled request may never be answered, so it is no longer in flight."""
        request = (message.get("params") or {}).get("requestId")
        self.session.in_flight.pop(_id_key(request), None)

    def _before_request(self, message: dict) -> None:
        if self._reload_due():
            self._stop_child()
        self.session.in_flight[_id_key(message["id"])] = message["id"]

    def _reload_due(self) -> bool:
        child = self.session.child
        if child is None or self.session.in_flight:
            return False
        return self._fingerprint(self._scripts) != child.fingerprint

    def _ensure_child(self, message: dict | None) -> None:
        if self.session.child is not None:
            return
        self._start_child()
        if (message or {}).get("method") != "initialize":
            self._replay_initialisation()

    def _start_child(self) -> None:
        self.session.generation += 1
        env = {**os.environ, WORKER_ENV: "1"}
        process = subprocess.Popen(  # noqa: S603 - our own interpreter and script
            self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env
        )
        child = Child(process, self.session.generation, self._fingerprint(self._scripts))
        self.session.child = child
        threading.Thread(target=self._read_child, args=(child,), daemon=True).start()

    def _replay_initialisation(self) -> None:
        if self.session.initialize is None:
            return
        replay = {**self.session.initialize, "id": f"{INIT_ID_PREFIX}{self.session.generation}"}
        self._write_child(json.dumps(replay).encode("utf-8") + b"\n")
        if self.session.initialized is not None:
            self._write_child(json.dumps(self.session.initialized).encode("utf-8") + b"\n")

    def _write_child(self, line: bytes) -> None:
        child = self.session.child
        if not line.endswith(b"\n"):
            line += b"\n"
        try:
            child.process.stdin.write(line)
            child.process.stdin.flush()
        except (BrokenPipeError, OSError):
            return  # its exit arrives as an event and answers what is in flight

    # --- child to client ------------------------------------------------

    def _on_child(self, payload: tuple[int, bytes]) -> None:
        generation, line = payload
        message = decoded(line)
        if generation != self.session.generation or self._is_private(message):
            return
        self._settle(message)
        self._write_client(line)

    def _settle(self, message: dict | None) -> None:
        if is_response(message):
            self.session.in_flight.pop(_id_key(message["id"]), None)

    @staticmethod
    def _is_private(message: dict | None) -> bool:
        identifier = (message or {}).get("id")
        return isinstance(identifier, str) and identifier.startswith(INIT_ID_PREFIX)

    def _on_child_exit(self, generation: int) -> None:
        child = self.session.child
        if child is None or child.generation != generation:
            return
        code = child.process.wait()
        for identifier in self.session.in_flight.values():
            self._write_client(error_line(identifier, f"memory server process exited with code {code}"))
        self.session.in_flight.clear()
        self.session.child = None

    def _write_client(self, line: bytes) -> None:
        if not line.endswith(b"\n"):
            line += b"\n"
        self._output.write(line)
        self._output.flush()

    # --- shutdown -------------------------------------------------------

    def _on_client_eof(self, _payload: None) -> bool:
        self._stop_child()
        return False

    def _stop_child(self) -> None:
        child = self.session.child
        self.session.child = None
        if child is None:
            return
        stop_process(child.process)


def stop_process(process: subprocess.Popen) -> None:
    """Close its input, wait, then terminate and kill if it will not go."""
    try:
        process.stdin.close()
    except OSError:
        pass
    steps = ((None, GRACEFUL_STOP_SECONDS), (process.terminate, STOP_SECONDS), (process.kill, STOP_SECONDS))
    for signal_it, seconds in steps:
        if _stopped_after(process, signal_it, seconds):
            return


def _stopped_after(
    process: subprocess.Popen, signal_it: Callable[[], None] | None, seconds: float
) -> bool:
    if signal_it is not None:
        signal_it()
    try:
        process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return False
    return True


def worker_command() -> list[str]:
    return [sys.executable, str(SCRIPTS / "mcp_server.py")]


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(
        prog="mcp_server.py",
        description=(
            "LLM Wiki MCP server over stdio. Registered with an MCP client, not run by "
            "hand; it serves the memory tools and reloads itself when its code changes."
        ),
    ).parse_args(argv)
    supervisor = Supervisor(worker_command(), SCRIPTS, sys.stdout.buffer)
    supervisor.feed_client(sys.stdin.buffer)
    return supervisor.run()
