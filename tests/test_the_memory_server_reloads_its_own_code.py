"""The stdio memory server reloads its code without the client noticing.

A server started at 10:41 on 2026-09-23 mixed its old `bounded_io` with a new
`evidence_graph` imported at 11:07 and answered `get_architecture` with
`operation_failed` until the session ended. See
docs/research/2026-09-24-the-memory-server-reloads-its-own-code.md.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import mcp_supervisor  # noqa: E402

from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402

FAKE_SERVER = '''
import json, os, sys
tag = open(os.environ["FAKE_TAG_FILE"]).read().strip()
inits = 0
ready = False
held = []
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        inits += 1
    if method == "notifications/initialized":
        ready = True
    if "id" not in message or "method" not in message:
        continue
    if method == "crash":
        sys.exit(3)
    if method == "hold":
        held.append(message["id"])
        continue
    answers = held + [message["id"]] if method == "release" else [message["id"]]
    held = [] if method == "release" else held
    for identifier in answers:
        result = {"tag": tag, "pid": os.getpid(), "inits": inits, "ready": ready}
        print(json.dumps({"jsonrpc": "2.0", "id": identifier, "result": result}), flush=True)
'''


class _Output:
    """What the supervisor writes to its client, one message at a time."""

    def __init__(self) -> None:
        self.lines: queue.Queue = queue.Queue()

    def write(self, line: bytes) -> None:
        self.lines.put(json.loads(line))

    def flush(self) -> None:
        return None

    def next(self) -> dict:
        return self.lines.get(timeout=SHORT_TIMEOUT)


class _Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tag = tmp_path / "tag"
        self.tag.write_text("A", encoding="utf-8")
        server = tmp_path / "fake_server.py"
        server.write_text(FAKE_SERVER, encoding="utf-8")
        self.output = _Output()
        self.supervisor = mcp_supervisor.Supervisor(
            [sys.executable, str(server)],
            tmp_path,
            self.output,
            fingerprint=lambda _scripts: self.tag.read_text(encoding="utf-8"),
        )
        read, write = os.pipe()
        self.client = os.fdopen(write, "wb", buffering=0)
        self.supervisor.feed_client(os.fdopen(read, "rb"))
        self.thread = threading.Thread(target=self.supervisor.run, daemon=True)
        self.thread.start()

    def send(self, message: dict) -> None:
        self.client.write(json.dumps({"jsonrpc": "2.0", **message}).encode("utf-8") + b"\n")

    def ask(self, identifier: object, method: str = "tools/call") -> dict:
        self.send({"id": identifier, "method": method})
        return self.output.next()

    def start(self) -> dict:
        answer = self.ask(1, "initialize")
        self.send({"method": "notifications/initialized"})
        return answer

    def close(self) -> None:
        self.client.close()
        self.thread.join(timeout=SHORT_TIMEOUT)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FAKE_TAG_FILE", str(tmp_path / "tag"))
    running = _Harness(tmp_path)
    yield running
    running.close()


def test_the_client_initialises_once_and_is_answered(harness: _Harness) -> None:
    first = harness.start()
    answer = harness.ask(2)

    assert (first["id"], answer["id"], answer["result"]["tag"]) == (1, 2, "A")
    assert answer["result"]["pid"] == first["result"]["pid"]


def test_changed_code_is_served_by_a_new_server_that_was_initialised_for_the_client(harness: _Harness) -> None:
    first = harness.start()
    harness.tag.write_text("B", encoding="utf-8")

    answer = harness.ask(3)

    result = answer["result"]
    assert (answer["id"], result["tag"], result["inits"], result["ready"]) == (3, "B", 1, True)
    assert result["pid"] != first["result"]["pid"]
    assert harness.output.lines.empty()


def test_nothing_reloads_while_a_request_is_in_flight(harness: _Harness) -> None:
    harness.start()
    harness.send({"id": 4, "method": "hold"})
    assert harness.ask(10)["result"]["tag"] == "A"  # the hold has been forwarded
    harness.tag.write_text("B", encoding="utf-8")

    answers = [harness.ask(5, "release"), harness.output.next()]

    assert sorted((item["id"], item["result"]["tag"]) for item in answers) == [(4, "A"), (5, "A")]


def test_a_cancelled_request_does_not_block_the_reload(harness: _Harness) -> None:
    harness.start()
    harness.send({"id": 8, "method": "hold"})
    harness.send({"method": "notifications/cancelled", "params": {"requestId": 8}})
    assert harness.ask(11)["result"]["tag"] == "A"  # the cancel has been seen
    harness.tag.write_text("B", encoding="utf-8")

    assert harness.ask(9)["result"]["tag"] == "B"


def test_a_server_that_exits_answers_what_was_in_flight_and_is_started_again(harness: _Harness) -> None:
    harness.start()

    crashed = harness.ask(6, "crash")
    after = harness.ask(7)

    assert (crashed["id"], crashed["error"]["code"]) == (6, mcp_supervisor.EXIT_ERROR_CODE)
    assert "exited with code 3" in crashed["error"]["message"]
    assert (after["id"], after["result"]["inits"], after["result"]["ready"]) == (7, 1, True)


def test_help_prints_usage_and_starts_nothing(capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        mcp_supervisor.main(["--help"])

    assert (stopped.value.code, "reloads itself" in capsys.readouterr().out) == (0, True)


def test_the_fingerprint_moves_when_a_source_file_changes(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text("a = 1\n", encoding="utf-8")
    before = mcp_supervisor.code_fingerprint(tmp_path)
    (tmp_path / "module.py").write_text("a = 22\n", encoding="utf-8")

    assert mcp_supervisor.code_fingerprint(tmp_path) != before


def _read_lines(stream, count: int) -> list[dict]:
    """`count` messages, or a failure after SHORT_TIMEOUT instead of a hung test."""
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: [lines.put(stream.readline()) for _ in range(count)], daemon=True).start()
    return [json.loads(lines.get(timeout=SHORT_TIMEOUT)) for _ in range(count)]


def test_the_real_server_answers_through_the_supervisor(tmp_path: Path) -> None:
    """End to end: the registered command, over stdio, lists the twelve tools."""
    env = {
        **os.environ,
        "LLM_WIKI_STATE_ROOT": str(tmp_path / "state"),
        "MEMORY_LLM_PROVIDER": "fake",
    }
    env.pop(mcp_supervisor.WORKER_ENV, None)
    process = subprocess.Popen(
        [sys.executable, str(SCRIPTS_DIR / "mcp_server.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    messages = [
        initialize,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    payload = b"".join(json.dumps(item).encode("utf-8") + b"\n" for item in messages)
    try:
        process.stdin.write(payload)
        process.stdin.flush()
        answers = _read_lines(process.stdout, 2)
    finally:
        mcp_supervisor.stop_process(process)

    tools = answers[1]["result"]["tools"]
    assert (answers[0]["id"], answers[1]["id"], len(tools)) == (1, 2, 12)
