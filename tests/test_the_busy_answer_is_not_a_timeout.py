"""A call that never started says so, and one boundary answers both transports.

Audit 3, K-B4 and K-B5. Research:
`docs/research/2026-09-17-one-bounded-tool-path-and-a-busy-answer-that-says-so.md`.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402


class _FakeServer:
    """The shape `_register_tools` needs: two decorator factories."""

    def __init__(self) -> None:
        self.callback = None

    def list_tools(self):
        return lambda callback: callback

    def call_tool(self, **_options):
        def register(callback):
            self.callback = callback
            return callback

        return register


def _registered_callback(mcp_server, monkeypatch):
    server = _FakeServer()
    monkeypatch.setattr(mcp_server, "MCP_CALL_TOOL_RESULT_AVAILABLE", False)
    monkeypatch.setattr(mcp_server, "MCP_STRUCTURED_OUTPUT_AVAILABLE", False)
    mcp_server._register_tools(server, [])
    return server.callback


def _envelope_text(result) -> str:
    """The JSON text out of whatever shape `_format_tool_result` returned."""
    if hasattr(result, "content"):
        return result.content[0].text
    if isinstance(result, tuple):
        return _envelope_text(result[0])
    return result[0].text


def _blocked_tool(release: threading.Event):
    def blocked(*, deadline):
        del deadline
        release.wait(SHORT_TIMEOUT)
        return {"ok": True}

    return blocked


def _busy_envelope(mcp_server, monkeypatch, call) -> dict:
    """Fill every worker slot, then read the answer one more call receives."""
    release = threading.Event()
    monkeypatch.setattr(mcp_server, "_MCP_WORKERS", set())
    monkeypatch.setattr(mcp_server, "_MCP_WORKERS_LOCK", threading.Lock())
    monkeypatch.setattr(mcp_server, "_vault_status", _blocked_tool(release))

    async def exercise():
        held = [
            asyncio.create_task(call("vault_status", {}))
            for _ in range(mcp_server.MCP_WORKER_SLOTS)
        ]
        await asyncio.sleep(0.05)
        refused = await call("vault_status", {})
        release.set()
        await asyncio.gather(*held)
        return refused

    try:
        return json.loads(asyncio.run(exercise()))
    finally:
        release.set()


def test_a_call_that_never_started_is_not_reported_as_a_timeout(monkeypatch):
    import mcp_server

    envelope = _busy_envelope(
        mcp_server, monkeypatch, mcp_server._handle_tool_call
    )

    assert (envelope["data"]["error"], envelope["warnings"]) == (
        "worker_capacity_exhausted",
        ["retry_after_running_calls_finish"],
    )


def test_the_registered_callback_refuses_a_full_queue_the_same_way(monkeypatch):
    import mcp_server

    callback = _registered_callback(mcp_server, monkeypatch)

    async def call(name, arguments):
        return _envelope_text(await callback(name, arguments))

    envelope = _busy_envelope(mcp_server, monkeypatch, call)

    assert envelope["data"]["error"] == "worker_capacity_exhausted"


def test_the_registered_callbacks_timeout_answer_is_made_when_it_times_out(monkeypatch):
    """Not at registration: a long-lived server used to answer with its start time."""
    import mcp_server

    callback = _registered_callback(mcp_server, monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(mcp_server, "MCP_OPERATION_SECONDS", 0.05)
    monkeypatch.setattr(mcp_server, "_MCP_WORKERS", set())
    monkeypatch.setattr(mcp_server, "_MCP_WORKERS_LOCK", threading.Lock())
    monkeypatch.setattr(mcp_server, "_vault_status", _blocked_tool(release))
    before = mcp_server.dt.datetime.now(mcp_server.dt.timezone.utc).isoformat()

    try:
        envelope = json.loads(
            _envelope_text(asyncio.run(callback("vault_status", {})))
        )
    finally:
        release.set()

    assert (envelope["data"]["error"], envelope["generated_at"] > before) == (
        "operation_timeout",
        True,
    )
