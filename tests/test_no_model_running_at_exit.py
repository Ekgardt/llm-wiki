"""A closing server waits for model inference to reach a safe point before it exits.

Exiting while a daemon thread was inside a PyTorch forward pass aborted the
process (exit 134). Research: `docs/research/2026-09-14-no-model-running-at-exit.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import inference_threads  # noqa: E402


def test_the_warm_up_does_not_start_a_stage_while_the_server_is_closing():
    import mcp_server

    ran: list[str] = []
    inference_threads.stopping.set()
    try:
        started = mcp_server.warmup_stage("pass_1", lambda: ran.append("pass"))
    finally:
        inference_threads.stopping.clear()

    assert (started, ran) == (False, [])
