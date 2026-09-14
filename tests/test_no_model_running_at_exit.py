"""A closing server waits for model inference to reach a safe point before it exits.

Exiting while a daemon thread was inside a PyTorch forward pass aborted the
process (exit 134). Research: `docs/research/2026-09-14-no-model-running-at-exit.md`.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import inference_threads  # noqa: E402

from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402


def test_settle_stops_inference_at_its_safe_point_and_waits_for_it():
    reached = threading.Event()

    def inference() -> None:
        reached.set()
        inference_threads.stopping.wait(timeout=SHORT_TIMEOUT)

    inference_threads.start(inference, name="test-inference")
    reached.wait(timeout=SHORT_TIMEOUT)

    assert (inference_threads.settle(5.0), inference_threads.running()) == ([], [])


def test_a_thread_that_will_not_stop_is_named_and_the_next_server_still_warms():
    release = threading.Event()
    stuck = inference_threads.start(lambda: release.wait(timeout=SHORT_TIMEOUT), name="stuck-inference")

    left = inference_threads.settle(0.05)
    still_asked = inference_threads.stopping.is_set()
    release.set()
    stuck.join(timeout=SHORT_TIMEOUT)

    assert (left, still_asked, inference_threads.stopping.is_set()) == (["stuck-inference"], True, False)


def test_the_warm_up_does_not_start_a_stage_while_the_server_is_closing():
    import mcp_server

    ran: list[str] = []
    inference_threads.stopping.set()
    try:
        started = mcp_server._warmup_stage("pass_1", lambda: ran.append("pass"))
    finally:
        inference_threads.stopping.clear()

    assert (started, ran) == (False, [])
