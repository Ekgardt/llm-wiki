"""Every get_architecture mode runs on the bounded code-graph workers under the deadline.

The non-summary modes parsed the tree on the tool's own thread with no deadline;
four hung calls held every MCP slot. See
docs/research/2026-09-25-every-architecture-mode-is-bounded.md.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import code_graph
import mcp_server

from tests.slow_machine import LONG_TIMEOUT


def test_a_hung_callers_query_answers_timeout_and_holds_no_tool_thread(tmp_path: Path, monkeypatch) -> None:
    released = threading.Event()

    def hangs(*_args, **_options):
        released.wait(LONG_TIMEOUT)
        return {"callers": []}

    monkeypatch.setattr(code_graph, "find_callers", hangs)
    started = time.monotonic()
    try:
        answer = mcp_server._get_architecture_mode(
            str(tmp_path), mode="callers", symbol="anything", deadline=time.monotonic() + 0.5
        )
    finally:
        released.set()

    assert (answer.get("status"), time.monotonic() - started < LONG_TIMEOUT) == ("timeout", True)
