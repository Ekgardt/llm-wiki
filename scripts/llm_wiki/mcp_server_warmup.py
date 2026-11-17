"""Warming the retrieval path and the reranker before the first question."""
from __future__ import annotations

import os
import sys
import threading
import time

# How long one warm-up question may run. It is not a budget the warm-up is
# expected to spend: a cold pass costs about 12 s here and a warm one about
# 1.3 s. Three minutes is "something is wrong", not "this is slow".
WARMUP_LIMIT_SECONDS = 180.0

# Two passes, and the second one is the whole point — see
# `warmup_retrieval_path`.
WARMUP_PASSES = 2


def warmup_pass(deadline_seconds: float) -> None:
    """One throwaway question down the real path, so nothing is warmed by proxy."""
    from retrieval import background_cancellation
    from search_memory import search

    search(
        "warm the retrieval path",
        limit=3,
        semantic=True,
        graph=True,
        rerank=True,
        source_tool="warmup",
        emit_telemetry=False,
        cancelled=background_cancellation(),
        deadline_monotonic=time.monotonic() + deadline_seconds,
    )


# What the warm-up did, for the health resource: not_started, running,
# warm (with seconds), or failed (stage and a redacted error). Research:
# docs/research/2026-09-10-a-failed-warm-up-is-in-the-health-answer.md
_WARMUP_LOCK = threading.Lock()
_WARMUP: dict[str, object] = {"status": "not_started"}


def warmup_state() -> dict[str, object]:
    with _WARMUP_LOCK:
        return dict(_WARMUP)


def _set_warmup(**fields: object) -> None:
    with _WARMUP_LOCK:
        _WARMUP.clear()
        _WARMUP.update(fields)


def _record_warmup_failure(stage: str, error: Exception) -> None:
    # The client sees the class, the server log the message: exception text
    # never crosses the MCP boundary (`_safe_exception_text`'s rule).
    _set_warmup(status="failed", stage=stage, error=type(error).__name__)
    print(
        f"mcp_server: retrieval warm-up failed at {stage}: {type(error).__name__}: {error}",
        file=sys.stderr,
    )


def _attempt_prioritized_warmup(run, deadline: float) -> bool:
    import inference_threads
    from retrieval import warmup_priority

    with warmup_priority(deadline, inference_threads.stopping.is_set) as cancelled:
        if not _run_unless_cancelled(run, cancelled):
            return False
    return True


def _run_unless_cancelled(run, cancelled) -> bool:
    from retrieval import require_optional_not_cancelled

    try:
        run()
        require_optional_not_cancelled(cancelled)
    except Exception:
        if cancelled():
            return False
        raise
    return True


def warmup_stage(stage: str, run, deadline_seconds: float = WARMUP_LIMIT_SECONDS) -> bool:
    """Only a complete idle pass warms; preempted work retries within its original clock."""
    import inference_threads

    deadline = time.monotonic() + deadline_seconds
    while not inference_threads.stopping.is_set():
        try:
            warmed = _attempt_prioritized_warmup(run, deadline)
        except Exception as error:  # noqa: BLE001 - recorded in the health answer
            _record_failure_unless_stopping(stage, error)
            return False
        if warmed:
            return True
    return False


def _record_failure_unless_stopping(stage: str, error: Exception) -> None:
    import inference_threads

    if inference_threads.stopping.is_set():
        return
    _record_warmup_failure(stage, error)


def warmup_retrieval_path(deadline_seconds: float = WARMUP_LIMIT_SECONDS) -> None:
    """Load the retrieval path and record what a *warm* optional stage costs.

    Loading the encoder is necessary and was not sufficient. `retrieval`
    admits an optional stage by comparing the last cost a finished run of that
    kind recorded against the window the caller can offer, and only a stage
    that actually ran records anything. So a warm-up that merely loaded the
    model left the cost unknown, the MCP budget's ~3.5 s window is below the
    ceiling `unknown_cost_stage_fits` requires, and the dense leg stayed
    refused until some later call happened to record a cost.

    Measured on this vault 2026-08-29, five `recall` calls in one process
    against the live generation: calls 1-3 answered `signals_used=['lexical']`,
    call 3 reading a recorded cost of 12.0 s — the cold load, clamped at the
    ceiling, standing in for a model that was resident by then. The dense leg
    first appeared on call 4.

    Hence two passes. The first pays the one-time load and records it; the
    second runs warm and replaces that figure with the steady-state cost, which
    is the only one an admission decision can use. That split — setup time as
    its own term rather than folded into service time — is what the queueing
    literature on cold starts does, and the arithmetic here is the same.

    It runs where the caller puts it: on a daemon thread for stdio, so serving
    starts immediately. A failure is never fatal — warming is an optimisation —
    but it is not silent either: the health resource names the stage and the
    error, because an unwarmed path answers from the lexical leg alone.
    """
    started = time.monotonic()
    _set_warmup(status="running")
    if not warmup_stage("reranker", warm_reranker, deadline_seconds):
        return
    for index in range(WARMUP_PASSES):
        if not warmup_stage(f"pass_{index + 1}", lambda: warmup_pass(deadline_seconds), deadline_seconds):
            return
    _set_warmup(status="warm", seconds=round(time.monotonic() - started, 3))


def warm_reranker() -> None:
    """Load the default cross-encoder before a question has to wait for it.

    The reranker is on by default since 2026-09-10 and reranks every
    question, so a resident server pays its 1.8 s load and the int8
    quantisation here, once, and never inside a question's optional-stage
    share. A vault with nothing to rerank yet still gets a resident model;
    the two passes below then record what a warm stage costs.
    """
    from reranker import reranker_available

    reranker_available()


def start_encoder_warmup() -> None:
    """Warm the retrieval path before the first question needs it.

    Measured on this vault (2026-08-24/26): a cold encoder costs 7.99 s and
    ~1.1 GiB resident, so the first semantic question of a session always fell
    back to lexical-only. The owner accepted that price on 2026-08-27: every
    resident server pays the memory up front, and the first `recall` answers
    with the dense leg in that measured corpus. This is not a guarantee for
    the current corpus or a cold fourteen-second answer. The load runs on a
    tracked background thread so serving starts
    immediately, and it shares `search_memory.get_embedder`'s module cache
    with the dense leg — a straggler racing it wastes one load, never a vector.
    Set LLMWIKI_NO_ENCODER_WARMUP=1 to keep the old lazy behaviour.

    What it loads is now the whole path rather than the encoder alone, because
    loading was never the part that was missing; `warmup_retrieval_path` says
    what was. Foreground search and its still-running workers now have
    priority: cooperating warm-up stages yield and retry only after actual
    settlement, within their original clocks. Native forwards are not killed.
    """
    if os.environ.get("LLMWIKI_NO_ENCODER_WARMUP") == "1":
        return

    import inference_threads

    inference_threads.start(warmup_retrieval_path, name="encoder-warmup")
