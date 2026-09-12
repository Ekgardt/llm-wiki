"""Deterministic subprocess fixture for Task 27 adapter integration tests."""

from __future__ import annotations

import json
import sys

ADAPTER_OFFSETS = {
    "grep-read": (0.50, 120),
    "graphify-pinned": (0.60, 100),
    "llm-wiki-current": (0.62, 95),
    "evidence-graph-only": (0.63, 92),
    "hybrid-retrieval": (0.66, 85),
    "adaptive-context-compiler": (0.68, 80),
}
METRICS = {
    "blinded_factual_correctness": None,
    "cache_tokens": None,
    "edge_precision": None,
    "edge_recall": None,
    "executable_task_success": None,
    "freshness": None,
    "incremental_time_ms": None,
    "index_size_bytes": None,
    "indexing_time_ms": None,
    "peak_ram_bytes": None,
    "query_latency_ms": None,
    "retrieval_quality": None,
    "uncached_input_tokens": None,
    "uncached_output_tokens": None,
}

_TRANSIENT_FAILURE_REQUEST = ("fixture-task-a", 1729, 1)


def _is_transient_failure(adapter_id: str, request: dict) -> bool:
    """One adapter fails once: for one task, one seed and the first attempt."""
    if adapter_id != "graphify-pinned":
        return False
    asked = (request["task_id"], request["seed"], request["attempt"])
    return asked == _TRANSIENT_FAILURE_REQUEST


def _failure_payload() -> dict:
    return {
        "failure": {
            "category": "backend",
            "code": "fixture-transient-failure",
            "message": "deterministic retry fixture",
            "phase": "query",
            "retryable": True,
        },
        "metrics": {
            **METRICS,
            "cache_tokens": 0,
            "uncached_input_tokens": 10,
            "uncached_output_tokens": 0,
        },
        "outcome": "failure",
    }


def _graph_metric(adapter_id: str, value: float) -> float | None:
    """Only the graph adapters report edge precision and recall."""
    if "graph" not in adapter_id:
        return None
    return value


def _success_payload(adapter_id: str) -> dict:
    quality, tokens = ADAPTER_OFFSETS[adapter_id]
    metrics = {
        **METRICS,
        "blinded_factual_correctness": quality,
        "cache_tokens": 0,
        "edge_precision": _graph_metric(adapter_id, 0.8),
        "edge_recall": _graph_metric(adapter_id, 0.75),
        "freshness": 1.0,
        "index_size_bytes": 1024,
        "indexing_time_ms": 10.0,
        "peak_ram_bytes": 4096,
        "query_latency_ms": 2.0,
        "retrieval_quality": quality,
        "uncached_input_tokens": tokens,
        "uncached_output_tokens": 10,
    }
    return {"failure": None, "metrics": metrics, "outcome": "success"}


def _response(adapter_id: str, request: dict) -> dict:
    if _is_transient_failure(adapter_id, request):
        return _failure_payload()
    return _success_payload(adapter_id)


def _probe_or_unknown(adapter_id: str) -> int | None:
    """The probe prints its model name; an unknown adapter is an error."""
    if adapter_id == "probe-model":
        print("fixture/model@v1")
        return 0
    if adapter_id not in ADAPTER_OFFSETS:
        return 2
    return None


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    adapter_id = sys.argv[1]
    early = _probe_or_unknown(adapter_id)
    if early is not None:
        return early
    print(json.dumps(_response(adapter_id, json.load(sys.stdin)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
