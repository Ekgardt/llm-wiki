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

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import mcp_supervisor  # noqa: E402

from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402


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
