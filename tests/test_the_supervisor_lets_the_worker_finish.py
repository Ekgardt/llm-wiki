"""The supervisor waits out the worker's own settling, and survives a file vanishing.

See docs/research/2026-09-25-the-supervisor-lets-the-worker-finish.md.
"""

from __future__ import annotations

import mcp_server
import mcp_supervisor


def test_the_graceful_stop_outlasts_the_workers_inference_wait() -> None:
    assert mcp_supervisor.GRACEFUL_STOP_SECONDS > mcp_server.SHUTDOWN_INFERENCE_SECONDS
