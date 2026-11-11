"""Deadlines, bounded worker threads and the timeout, busy and refusal envelopes."""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import datetime as dt
import inspect
import json
import sys
import threading
import time

from llm_wiki import mcp_server_limits
from llm_wiki.mcp_server_limits import (
    _MCP_WORKER_IDS,
    _OPERATION_CANCELLED,
    _OPERATION_DEADLINE,
    MCP_REPOSITORY_INDEX_SECONDS,
    MCP_WORKER_SLOTS,
    PRECISE_ARCHITECTURE_MODES,
)


def bounded_operation_deadline(deadline: float | None = None) -> float:
    if deadline is not None:
        return deadline
    inherited = _OPERATION_DEADLINE.get()
    return inherited if inherited is not None else time.monotonic() + mcp_server_limits.MCP_OPERATION_SECONDS


def check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("MCP operation deadline reached")


def _positioned_architecture_call(arguments: dict, mode: str) -> bool:
    if mode not in {"callers", "callees"}:
        return False
    return all(key in arguments for key in ("path", "line", "character"))


_LONG_ARCHITECTURE_BUDGETS = {"index": MCP_REPOSITORY_INDEX_SECONDS}


def tool_operation_seconds(name: str, arguments: object) -> float:
    """The budget a tool call gets: the default, unless its tool names a longer one."""
    budget = _TOOL_BUDGETS.get(name)
    if budget is None or not isinstance(arguments, dict):
        return mcp_server_limits.MCP_OPERATION_SECONDS
    return budget(arguments)


def _architecture_operation_seconds(arguments: dict) -> float:
    if is_precise_architecture_request(arguments):
        return mcp_server_limits.MCP_LSP_STARTUP_SECONDS
    return _LONG_ARCHITECTURE_BUDGETS.get(
        arguments.get("mode", "summary"), mcp_server_limits.MCP_OPERATION_SECONDS
    )


def retrieval_operation_seconds(_arguments: dict | None = None) -> float:
    """A retrieval answer waits for its reranker: `mcp.retrieval_seconds` (B-9,
    docs/research/2026-09-27-a-rerank-has-the-window-it-was-measured-to-need.md)."""
    from settings import setting_value

    return float(setting_value("mcp.retrieval_seconds"))


def _recall_operation_seconds(arguments: dict) -> float:
    """A grounded answer waits on a provider: one round trip measured 32.5 s (A-17)."""
    if arguments.get("grounded") is not True:
        return retrieval_operation_seconds()
    from query_memory import QA_DEADLINE_SECONDS

    return QA_DEADLINE_SECONDS


_TOOL_BUDGETS = {
    "get_architecture": _architecture_operation_seconds,
    "recall": _recall_operation_seconds,
    "get_decisions": retrieval_operation_seconds,
}


def operation_cancelled():
    cancelled = _OPERATION_CANCELLED.get()
    return cancelled if callable(cancelled) else lambda: False


def _call_with_deadline(function, *args, deadline: float, **kwargs):
    """Pass deadlines to helpers that explicitly support them."""
    try:
        accepts_deadline = "deadline" in inspect.signature(function).parameters
    except (TypeError, ValueError):
        accepts_deadline = False
    if accepts_deadline:
        kwargs["deadline"] = deadline
    return function(*args, **kwargs)


def _cancellation_context():
    """Copy the caller context with the abandonment flag already bound in it."""
    abandoned = threading.Event()
    cancellation_token = _OPERATION_CANCELLED.set(abandoned.is_set)
    try:
        return abandoned, contextvars.copy_context()
    finally:
        _OPERATION_CANCELLED.reset(cancellation_token)


class _WorkerCapacityExhausted(TimeoutError):
    """Every worker slot was busy, so this call never started.

    A `TimeoutError` so that every caller which already bounds a call keeps
    working, but a distinct one: a lapse means the work ran and did not finish,
    this means it never ran and an immediate retry is reasonable (audit 3, B4).
    """


def _reserve_mcp_worker(submitted) -> None:
    with mcp_server_limits.MCP_WORKERS_LOCK:
        mcp_server_limits.MCP_WORKERS.difference_update(
            future for future in mcp_server_limits.MCP_WORKERS if future.done()
        )
        if len(mcp_server_limits.MCP_WORKERS) >= MCP_WORKER_SLOTS:
            raise _WorkerCapacityExhausted("MCP worker capacity exhausted")
        mcp_server_limits.MCP_WORKERS.add(submitted)


def _completed_worker_error(completed):
    try:
        return completed.exception()
    except concurrent.futures.CancelledError:
        return None


def _discard_mcp_worker(completed, abandoned) -> None:
    error = _completed_worker_error(completed)
    if error is not None and abandoned.is_set():
        print(
            f"mcp worker failed after caller timeout: {type(error).__name__}",
            file=sys.stderr,
        )
    with mcp_server_limits.MCP_WORKERS_LOCK:
        mcp_server_limits.MCP_WORKERS.discard(completed)


def _worker_discarder(abandoned):
    def discard(completed):
        _discard_mcp_worker(completed, abandoned)

    return discard


def _run_submitted(submitted, context, function, args) -> None:
    if not submitted.set_running_or_notify_cancel():
        return
    try:
        result = context.run(function, *args)
    except BaseException as error:
        submitted.set_exception(error)
    else:
        submitted.set_result(result)


def _start_worker_thread(submitted, context, function, args) -> None:
    thread = threading.Thread(
        target=_run_submitted,
        args=(submitted, context, function, args),
        name=f"llm-wiki-mcp-{next(_MCP_WORKER_IDS)}",
        daemon=True,
    )
    try:
        thread.start()
    except BaseException:
        submitted.cancel()
        raise


# ``code_graph``'s live extraction takes no deadline and cannot be interrupted
# from outside, so an abandoned run keeps its worker until it finishes on its
# own. The slot cap keeps repeated timeouts from stacking runaway workers.
CODE_GRAPH_WORK_SLOTS = 2

CODE_GRAPH_SLOTS = threading.Semaphore(CODE_GRAPH_WORK_SLOTS)


def _run_code_graph_work(function, args, kwargs, outcome) -> None:
    try:
        outcome.set_result(function(*args, **kwargs))
    except BaseException as error:  # noqa: BLE001 - delivered to the waiting caller
        outcome.set_exception(error)
    finally:
        CODE_GRAPH_SLOTS.release()


def _started_code_graph_worker(function, args, kwargs):
    """Start abandonable code-graph work on its own bounded daemon worker."""
    if not CODE_GRAPH_SLOTS.acquire(blocking=False):
        raise TimeoutError(
            "code graph workers are still busy with previously abandoned work"
        )
    outcome = concurrent.futures.Future()
    thread = threading.Thread(
        target=_run_code_graph_work,
        args=(function, args, kwargs, outcome),
        name="llm-wiki-code-graph",
        daemon=True,
    )
    try:
        thread.start()
    except BaseException:
        CODE_GRAPH_SLOTS.release()
        raise
    return outcome


def _bounded_code_graph_call(function, *args, deadline: float, **kwargs):
    """Bound non-cooperative code-graph work by abandoning it at the deadline."""
    check_deadline(deadline)
    outcome = _started_code_graph_worker(function, args, kwargs)
    try:
        return outcome.result(timeout=max(0.0, deadline - time.monotonic()))
    except concurrent.futures.TimeoutError:
        raise TimeoutError(
            "code graph analysis exceeded the operation deadline"
        ) from None


def _code_graph_timeout_data(
    directory: object, error: BaseException, *, completed: tuple[str, ...]
) -> dict:
    """A bounded, named result for a code-graph budget that ran out."""
    return {
        "directory": directory if isinstance(directory, str) else None,
        "status": "timeout",
        "warning": "code_graph_timeout",
        "detail": str(error),
        "completed": list(completed),
        "skipped": ["code_graph_analysis"],
    }


async def run_bounded(function, *args, deadline: float):
    """Run synchronous work off-loop without an unbounded submission queue."""
    check_deadline(deadline)
    submitted = concurrent.futures.Future()
    abandoned, context = _cancellation_context()
    _reserve_mcp_worker(submitted)
    submitted.add_done_callback(_worker_discarder(abandoned))
    _start_worker_thread(submitted, context, function, args)
    future = asyncio.wrap_future(submitted)
    try:
        done, _pending = await asyncio.wait(
            {future}, timeout=max(0.0, deadline - time.monotonic())
        )
    except asyncio.CancelledError:
        abandoned.set()
        future.cancel()
        raise
    if not done:
        abandoned.set()
        future.cancel()
        raise TimeoutError("MCP operation deadline reached")
    return future.result()


def timeout_envelope_text() -> str:
    return _refused_envelope_text("operation_timeout", "operation_timeout")


def _busy_envelope_text() -> str:
    """The answer to a call that never started because no worker slot was free."""
    return _refused_envelope_text(
        "worker_capacity_exhausted", "retry_after_running_calls_finish"
    )


def _refused_envelope_text(error: str, warning: str) -> str:
    envelope = {
        "schema_version": "1.0",
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "index_timestamp": None,
        "source_commit": None,
        "freshness": "unknown",
        "coverage": 0.0,
        "confidence": 0.2,
        "fallback": False,
        "partial": True,
        "warnings": [warning],
        "components": {},
        "data": {"error": error},
    }
    return rendered_envelope(envelope)


def rendered_envelope(envelope: dict) -> str:
    """One answer, as compactly as JSON allows.

    Indentation is billed like content and read by nobody: measured 2026-09-13,
    `mode=callers` for one symbol cost 836 tokens with `indent=2` and 608 without
    it, the same facts either way. Research:
    `docs/research/2026-09-13-a-shorter-answer-and-a-fresher-line.md`.
    """

    return json.dumps(
        envelope, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def is_precise_architecture_request(arguments: dict) -> bool:
    mode = arguments.get("mode", "summary")
    if mode in PRECISE_ARCHITECTURE_MODES:
        return True
    return _positioned_architecture_call(arguments, mode)
