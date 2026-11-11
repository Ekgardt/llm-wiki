"""Helpers shared by test_code_navigation_benchmark.py and the test files importing them."""
from __future__ import annotations

from navigation_benchmark_metrics import precision_recall_f1

PACKAGE_SHA256 = "bd5c488fc20fa237a944279bf32cae2f986cf10d5d5d9e8705819859daeb2f4a"


def _metric(tp: int, fp: int, fn: int) -> dict[str, int | float]:
    return precision_recall_f1(
        {("expected", index) for index in range(tp + fn)},
        {
            *(("expected", index) for index in range(tp)),
            *(("actual", index) for index in range(fp)),
        },
    )


def _gold_query_ids() -> list[str]:
    return [
        *(f"definition-{index:03d}" for index in range(200)),
        *(f"references-{index:03d}" for index in range(100)),
        *(f"calls-{index:03d}" for index in range(100)),
    ]


_OWNERSHIP_SCENARIOS = ("normal_shutdown", "crash", "timeout", "cancellation")


def _ownership_section() -> dict:
    return {
        scenario: {"available": True, "orphan_count": 0}
        for scenario in _OWNERSHIP_SCENARIOS
    }


def _token_tasks() -> list:
    return [
        {
            "query_id": query_id,
            "uncached_input_tokens": 10,
            "cache_read_tokens": 0,
            "raw_tool_tokens": 20,
            "output_tokens": 15,
        }
        for query_id in _gold_query_ids()[:396]
    ]


def _performance_section(qualification: bool) -> dict:
    """A correctness-only run measures no latency at all."""
    if not qualification:
        return {
            "available": False,
            "cold_readiness_seconds": None,
            "warm_facade_p50_ms": None,
            "warm_facade_p95_ms": None,
            "direct_pyright_p95_ms": None,
            "warm_overhead_p95_ms": None,
            "sample_count": 0,
        }
    return {
        "available": True,
        "cold_readiness_seconds": 30.0,
        "warm_facade_p50_ms": 5.0,
        "warm_facade_p95_ms": 8.0,
        "direct_pyright_p95_ms": 3.0,
        "warm_overhead_p95_ms": 5.0,
        "sample_count": 20,
    }


def _resources_section(qualification: bool) -> dict:
    if not qualification:
        return {
            "available": False,
            "client_peak_rss_mib": None,
            "method": "not_measured_in_correctness_only",
        }
    return {
        "available": True,
        "client_peak_rss_mib": 60.0,
        "method": "measured-test",
    }


def _measured_report(mode: str = "qualification") -> dict[str, object]:
    qualification = mode == "qualification"
    return {
        "schema_version": "code-navigation-python-report/v1",
        "mode": mode,
        "identity": {
            "source_manifest_sha256": "30f0b042afc971426f9e0c8e6fa8f1c9446976de82a427acda3d1ec9eb0e31e8",
            "gold_sha256": "da2ddb5b6ef245a388b7fa4142770aa25e0580319df16af6ac7475a3c0455a5f",
            "git_commit": "c" * 40,
            "python_version": "3.14.14",
            "pyright_version": "1.1.411",
            "pyright_package_sha256": PACKAGE_SHA256,
            "node_version": "v22.15.0",
            "node_major": 22,
        },
        "environment": {
            "os": "Linux",
            "os_version": "1",
            "architecture": "test-arch",
            "cpu_model": "test-cpu",
            "cpu_core_count": 8,
            "ram_class": "16-31 GiB",
        },
        "workload": {
            "fixture_seed": 411,
            "fixture_lines": 100_000,
            "definition_queries": 200,
            "reference_queries": 100,
            "call_queries": 100,
            "edit_rename_delete_cycles": 50,
            "crash_cycles": 20,
            "default_limit": 10,
            "max_estimated_tokens": 1200,
            "freshness_checks": 250,
            "ownership_checks": 4,
            "ownership_scenarios": [
                "normal_shutdown",
                "crash",
                "timeout",
                "cancellation",
            ],
        },
        "evidence": {
            "measured": True,
            "runner": "code-navigation-real/v1",
            "source_hash_verified": True,
            "gold_hash_verified": True,
            "git_commit_verified": True,
            "identity_verified": True,
            "query_attempts": 400,
            "mutation_cycles": 50,
            "crash_attempts": 20,
            "ownership_checks": 4,
        },
        "correctness": {
            "definitions": {"attempted": 200, "exact": 198, "accuracy": 0.99},
            "references": {"attempted": 100, **_metric(475, 25, 25)},
            "calls": {"attempted": 100, **_metric(100, 0, 0)},
            "task_success_rate": 0.99,
            "citation_locations_attempted": 800,
            "citation_locations_correct": 800,
            "citation_correctness_rate": 1.0,
        },
        "tokens": {
            "cache_read_label": "not_applicable_no_result_cache",
            "tasks": _token_tasks(),
            "default_items": 10,
            "max_default_estimated_tokens": 1200,
        },
        "reliability": {
            "stale_answer_count": 0,
            "freshness_checks_attempted": 250,
            "freshness_checks_measured": 250,
            "stale_result_rate": 0.0,
            "mutation_cycles_measured": 50,
            "edit_to_fresh_p50_ms": 2.0,
            "edit_to_fresh_p95_ms": 4.0,
            "crash_recoveries": 20,
            "crash_attempts": 20,
            "recovery_rate": 1.0,
            "orphan_process_count": 0,
            "orphan_checks_attempted": 4,
            "orphan_checks_measured": 4,
            "orphan_process_rate": 0.0,
            "ownership": _ownership_section(),
        },
        "performance": _performance_section(qualification),
        "resources": _resources_section(qualification),
        "operator_corpus": None,
        "errors": [],
        "market_superiority_claimed": False,
    }
