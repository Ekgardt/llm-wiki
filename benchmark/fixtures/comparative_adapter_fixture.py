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


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    adapter_id = sys.argv[1]
    if adapter_id == "probe-model":
        print("fixture/model@v1")
        return 0
    if adapter_id not in ADAPTER_OFFSETS:
        return 2
    request = json.load(sys.stdin)
    if (
        adapter_id == "graphify-pinned"
        and request["task_id"] == "fixture-task-a"
        and request["seed"] == 1729
        and request["attempt"] == 1
    ):
        print(json.dumps({
            "failure": {
                "category": "backend",
                "code": "fixture-transient-failure",
                "message": "deterministic retry fixture",
                "phase": "query",
                "retryable": True,
            },
            "metrics": {**METRICS, "cache_tokens": 0, "uncached_input_tokens": 10,
                        "uncached_output_tokens": 0},
            "outcome": "failure",
        }, sort_keys=True))
        return 0
    quality, tokens = ADAPTER_OFFSETS[adapter_id]
    metrics = {
        **METRICS,
        "blinded_factual_correctness": quality,
        "cache_tokens": 0,
        "edge_precision": 0.8 if "graph" in adapter_id else None,
        "edge_recall": 0.75 if "graph" in adapter_id else None,
        "freshness": 1.0,
        "index_size_bytes": 1024,
        "indexing_time_ms": 10.0,
        "peak_ram_bytes": 4096,
        "query_latency_ms": 2.0,
        "retrieval_quality": quality,
        "uncached_input_tokens": tokens,
        "uncached_output_tokens": 10,
    }
    print(json.dumps({"failure": None, "metrics": metrics, "outcome": "success"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
