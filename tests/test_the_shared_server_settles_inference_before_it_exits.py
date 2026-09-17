"""Audit 3, A8: the HTTP transport leaves no model mid-inference at exit either.

Research: `docs/research/2026-09-17-the-shared-server-settles-inference-too.md`.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import inference_threads  # noqa: E402
import mcp_http  # noqa: E402

from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402


def test_shutdown_waits_for_inference_to_reach_its_safe_point():
    reached = threading.Event()
    stopped_when_asked = []

    def inference() -> None:
        reached.set()
        stopped_when_asked.append(inference_threads.stopping.wait(timeout=SHORT_TIMEOUT))

    inference_threads.start(inference, name="shared-server-inference")
    reached.wait(timeout=SHORT_TIMEOUT)

    mcp_http._shutdown()

    assert (stopped_when_asked, inference_threads.running()) == ([True], [])
