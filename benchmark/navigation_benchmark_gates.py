"""Evidence completeness checks and the qualification gates over a measured report."""

from __future__ import annotations

import math
import re
import sys
from collections.abc import Mapping
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
BENCHMARK_ROOT = Path(__file__).resolve().parent
for _path in (SCRIPTS_ROOT, BENCHMARK_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from generate_python_qualification import (  # noqa: E402
    CALL_QUERIES,
    DEFINITION_QUERIES,
    REFERENCE_QUERIES,
)
from navigation_benchmark_metrics import (  # noqa: E402
    PERFORMANCE_SAMPLES,
    _prf_from_counts,
)
from navigation_benchmark_ownership import _OWNERSHIP_SCENARIOS  # noqa: E402
from navigation_benchmark_schema import (  # noqa: E402
    _finite_number,
    _is_count,
    load_manifest,
    validate_report,
)

_PYTHON_314_VERSION = re.compile(r"3\.14\.(?:0|[1-9][0-9]*)\Z")

GATE_THRESHOLDS: dict[str, float | int] = {
    "definition_accuracy": 0.99,
    "reference_f1": 0.95,
    "stale_answer_count": 0,
    "stale_result_rate": 0.0,
    "orphan_process_count": 0,
    "orphan_process_rate": 0.0,
    "recovery_rate": 1.0,
    "default_items": 10,
    "default_estimated_tokens": 1200,
    # The floor, not the whole bound: see WARM_OVERHEAD_MAX_SHARE_OF_DIRECT.
    # 30 ms, not 20: three consecutive GitHub-hosted four-vCPU runs measured
    # 22.8, 22.08, and 22.16 ms. The facade walks the workspace revision an
    # extra time to guarantee freshness, and that cost is real on a slow
    # machine. Approved by the operator 2026-08-19; see
    # knowledge/notes/warm-navigation-overhead-threshold-decision.md.
    "warm_overhead_p95_ms": 30,
    "cold_readiness_seconds": 60,
    "client_rss_mib": 100,
}

# Above the floor the warm-overhead bound scales with the run's own control
# measurement: our layer may take up to this share of the time Pyright itself
# takes at p95, on the same queries, in the same run. A shared runner having a
# slow afternoon moves both sides and cannot fail the gate on its own; our
# layer taking a bigger share of the same work still does. Baseline 0.721 and
# 0.738 on 2026-09-18, noise floor 2.4%; see
# docs/research/2026-09-18-the-gate-measures-our-share-not-the-machine.md.
WARM_OVERHEAD_MAX_SHARE_OF_DIRECT = 0.90

_CORRECTNESS_GATES = (
    "definition_accuracy",
    "reference_f1",
    "stale_answer_count",
    "stale_result_rate",
    "orphan_process_count",
    "orphan_process_rate",
    "recovery_rate",
    "default_items",
    "default_estimated_tokens",
)
_QUALIFICATION_GATES = (
    *_CORRECTNESS_GATES,
    "warm_overhead_p95_ms",
    "cold_readiness_seconds",
    "client_rss_mib",
)


def _ratio_equal(actual: object, expected: float) -> bool:
    return (
        _finite_number(actual)
        and math.isfinite(expected)
        and math.isclose(
            float(actual),
            expected,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
    )


def _set_metric_consistent(metric: Mapping[str, object]) -> bool:
    counts = (metric["true_positive"], metric["false_positive"], metric["false_negative"])
    if any(not _is_count(value) for value in counts):
        return False
    precision, recall, f1 = _prf_from_counts(*counts)
    return _ratio_equal(metric["precision"], precision) and _ratio_equal(metric["recall"], recall) and _ratio_equal(metric["f1"], f1)


def _identity_complete(report: Mapping[str, object], manifest: Mapping[str, object]) -> bool:
    identity = report["identity"]
    return (
        identity["source_manifest_sha256"] == manifest["expected_source_manifest_sha256"]
        and identity["gold_sha256"] == manifest["expected_gold_sha256"]
    )


def _evidence_section_complete(evidence: Mapping[str, object]) -> bool:
    flags = all(
        evidence[key] is True
        for key in ("measured", "source_hash_verified", "gold_hash_verified", "git_commit_verified", "identity_verified")
    )
    counts = (evidence["query_attempts"], evidence["mutation_cycles"], evidence["crash_attempts"], evidence["ownership_checks"])
    return flags and counts == (400, 50, 20, 4)


def _definitions_complete(definitions: Mapping[str, object]) -> bool:
    return (
        definitions["attempted"] == 200
        and definitions["exact"] <= definitions["attempted"]
        and _ratio_equal(definitions["accuracy"], definitions["exact"] / definitions["attempted"])
    )


def _reference_metrics_complete(references: Mapping[str, object]) -> bool:
    return (
        references["attempted"] == 100
        and _set_metric_consistent(references)
        and references["true_positive"] + references["false_negative"] == 500
    )


def _call_metrics_complete(calls: Mapping[str, object]) -> bool:
    return calls["attempted"] == 100 and _set_metric_consistent(calls) and calls["true_positive"] + calls["false_negative"] == 100


def _query_metrics_complete(correctness: Mapping[str, object]) -> bool:
    return (
        _definitions_complete(correctness["definitions"])
        and _reference_metrics_complete(correctness["references"])
        and _call_metrics_complete(correctness["calls"])
    )


def _required_citation_locations(token_query_ids: list[str]) -> int:
    return sum(5 if query_id.startswith("references-") else 1 for query_id in token_query_ids)


def _citations_complete(correctness: Mapping[str, object], required: int) -> bool:
    attempted = correctness["citation_locations_attempted"]
    correct = correctness["citation_locations_correct"]
    if attempted <= 0 or correct > attempted:
        return False
    if attempted < required or correct < required:
        return False
    return _ratio_equal(correctness["citation_correctness_rate"], correct / attempted)


def _correctness_complete(report: Mapping[str, object]) -> bool:
    correctness = report["correctness"]
    token_tasks = report["tokens"]["tasks"]
    ids = [task["query_id"] for task in token_tasks]
    return (
        _query_metrics_complete(correctness)
        and _ratio_equal(correctness["task_success_rate"], len(token_tasks) / 400)
        and _citations_complete(correctness, _required_citation_locations(ids))
    )


def _valid_query_ids() -> set[str]:
    return {
        *(f"definition-{index:03d}" for index in range(DEFINITION_QUERIES)),
        *(f"references-{index:03d}" for index in range(REFERENCE_QUERIES)),
        *(f"calls-{index:03d}" for index in range(CALL_QUERIES)),
    }


def _ids_valid(ids: list[str]) -> bool:
    return len(ids) == len(set(ids)) and set(ids) <= _valid_query_ids()


def _tokens_complete(report: Mapping[str, object]) -> bool:
    tokens = report["tokens"]
    ids = [task["query_id"] for task in tokens["tasks"]]
    return (
        _ids_valid(ids)
        and tokens["cache_read_label"] == "not_applicable_no_result_cache"
        and all(task["cache_read_tokens"] == 0 for task in tokens["tasks"])
    )


def _freshness_complete(reliability: Mapping[str, object]) -> bool:
    return (
        reliability["freshness_checks_attempted"] == 250
        and reliability["freshness_checks_measured"] == 250
        and reliability["stale_answer_count"] <= 250
        and _ratio_equal(reliability["stale_result_rate"], reliability["stale_answer_count"] / 250)
        and reliability["mutation_cycles_measured"] == 50
    )


def _latency_complete(reliability: Mapping[str, object]) -> bool:
    p50 = reliability["edit_to_fresh_p50_ms"]
    p95 = reliability["edit_to_fresh_p95_ms"]
    return _finite_number(p50) and p50 >= 0 and _finite_number(p95) and p95 >= 0 and p50 <= p95


def _crash_complete(reliability: Mapping[str, object]) -> bool:
    return (
        reliability["crash_attempts"] == 20
        and reliability["crash_recoveries"] <= reliability["crash_attempts"]
        and _ratio_equal(reliability["recovery_rate"], reliability["crash_recoveries"] / reliability["crash_attempts"])
    )


def _ownership_measured(ownership: Mapping[str, Mapping[str, object]]) -> bool:
    return all(
        ownership[scenario]["available"] is True and ownership[scenario]["orphan_count"] is not None
        for scenario in _OWNERSHIP_SCENARIOS
    )


def _orphan_totals_consistent(reliability: Mapping[str, object], total: int) -> bool:
    return reliability["orphan_process_count"] == total and _ratio_equal(reliability["orphan_process_rate"], total / 4)


def _ownership_complete(reliability: Mapping[str, object]) -> bool:
    ownership = reliability["ownership"]
    total = sum(ownership[scenario]["orphan_count"] for scenario in _OWNERSHIP_SCENARIOS)
    return (
        reliability["orphan_checks_attempted"] == 4
        and reliability["orphan_checks_measured"] == 4
        and _ownership_measured(ownership)
        and _orphan_totals_consistent(reliability, total)
    )


def _reliability_complete(report: Mapping[str, object]) -> bool:
    reliability = report["reliability"]
    return (
        _freshness_complete(reliability)
        and _latency_complete(reliability)
        and _crash_complete(reliability)
        and _ownership_complete(reliability)
    )


def _operator_corpus_complete(report: Mapping[str, object]) -> bool:
    operator_corpus = report["operator_corpus"]
    if operator_corpus is None:
        return True
    return operator_corpus["available"] is True and operator_corpus["errors"] == 0


_PERFORMANCE_FIELDS = (
    "cold_readiness_seconds",
    "warm_facade_p50_ms",
    "warm_facade_p95_ms",
    "direct_pyright_p95_ms",
    "warm_overhead_p95_ms",
)


def _positive_number(value: object) -> bool:
    return _finite_number(value) and float(value) > 0.0  # type: ignore[arg-type]


def _performance_measured(performance: Mapping[str, object]) -> bool:
    """Every latency the report promises is a finite number, and the control is positive."""
    return all(_finite_number(performance[field]) for field in _PERFORMANCE_FIELDS) and _positive_number(
        performance["direct_pyright_p95_ms"]
    )


def _performance_complete(performance: Mapping[str, object]) -> bool:
    return (
        performance["available"] is True
        and performance["sample_count"] == PERFORMANCE_SAMPLES
        and _performance_measured(performance)
        and performance["warm_facade_p50_ms"] <= performance["warm_facade_p95_ms"]
    )


def _resources_complete(resources: Mapping[str, object]) -> bool:
    return resources["available"] is True and _finite_number(resources["client_peak_rss_mib"]) and resources["method"] != "unavailable"


def _python_314(identity: Mapping[str, object]) -> bool:
    version = identity["python_version"]
    return isinstance(version, str) and _PYTHON_314_VERSION.fullmatch(version) is not None


def _qualification_complete(report: Mapping[str, object]) -> bool:
    if report["mode"] != "qualification":
        return True
    return (
        report["environment"]["os"] == "Linux"
        and _python_314(report["identity"])
        and _performance_complete(report["performance"])
        and _resources_complete(report["resources"])
    )


def _evidence_complete(report: Mapping[str, object]) -> bool:
    try:
        manifest = load_manifest()
        checks = (
            _identity_complete(report, manifest),
            _evidence_section_complete(report["evidence"]),
            _correctness_complete(report),
            _tokens_complete(report),
            _reliability_complete(report),
            report["errors"] == [],
            _operator_corpus_complete(report),
            _qualification_complete(report),
        )
        return all(checks)
    except (ArithmeticError, KeyError, TypeError, ValueError):
        return False


def _gate_values(report: Mapping[str, object]) -> dict[str, object]:
    correctness = report["correctness"]
    reliability = report["reliability"]
    tokens = report["tokens"]
    performance = report["performance"]
    resources = report["resources"]
    return {
        "definition_accuracy": correctness["definitions"]["accuracy"],
        "reference_f1": correctness["references"]["f1"],
        "stale_answer_count": reliability["stale_answer_count"],
        "stale_result_rate": reliability["stale_result_rate"],
        "orphan_process_count": reliability["orphan_process_count"],
        "orphan_process_rate": reliability["orphan_process_rate"],
        "recovery_rate": reliability["recovery_rate"],
        "default_items": tokens["default_items"],
        "default_estimated_tokens": tokens["max_default_estimated_tokens"],
        "warm_overhead_p95_ms": performance["warm_overhead_p95_ms"],
        "cold_readiness_seconds": performance["cold_readiness_seconds"],
        "client_rss_mib": resources["client_peak_rss_mib"],
    }


_MINIMUM_GATES = frozenset({"definition_accuracy", "reference_f1", "recovery_rate"})


def _invalid_evaluation() -> dict[str, object]:
    return {"passed": False, "schema_valid": False, "evidence_complete": False, "scope": "invalid", "gates": {}}


def _gate_value_measured(value: object, measured: bool) -> bool:
    if not measured or isinstance(value, bool):
        return False
    return isinstance(value, (int, float))


def _gate_passed(field: str, value: object, threshold: object, measured: bool) -> bool:
    if not _gate_value_measured(value, measured):
        return False
    return _meets_threshold(field, float(value), float(threshold))


def _meets_threshold(field: str, value: float, threshold: float) -> bool:
    if field in _MINIMUM_GATES:
        return value >= threshold
    if field == "client_rss_mib":
        return value < threshold
    return value <= threshold


def _warm_overhead_threshold(direct_pyright_p95_ms: object) -> float | int:
    """The floor, or the run's own control measurement scaled — whichever is larger."""
    floor = GATE_THRESHOLDS["warm_overhead_p95_ms"]
    if not _positive_number(direct_pyright_p95_ms):
        return floor
    return max(float(floor), WARM_OVERHEAD_MAX_SHARE_OF_DIRECT * float(direct_pyright_p95_ms))  # type: ignore[arg-type]


def _run_thresholds(performance: Mapping[str, object]) -> dict[str, float | int]:
    """Production thresholds for one run: the warm-overhead bound carries its control."""
    thresholds = dict(GATE_THRESHOLDS)
    thresholds["warm_overhead_p95_ms"] = _warm_overhead_threshold(performance["direct_pyright_p95_ms"])
    return thresholds


def _gate_entry(field: str, value: object, complete: bool, thresholds: Mapping[str, float | int]) -> dict[str, object]:
    threshold = thresholds[field]
    measured = complete and value is not None
    return {
        "measured": measured,
        "value": value,
        "threshold": threshold,
        "passed": _gate_passed(field, value, threshold, measured),
    }


def _evaluation(complete: bool, gates: dict, qualification: bool) -> dict[str, object]:
    return {
        "passed": complete and all(item["passed"] for item in gates.values()),
        "schema_valid": True,
        "evidence_complete": complete,
        "scope": "qualification" if qualification else "correctness_reliability",
        "gates": gates,
    }


def evaluate_gates(report: object) -> dict[str, object]:
    """Fail closed unless a schema-valid measured report has complete evidence."""
    try:
        validate_report(report)
    except (TypeError, ValueError):
        return _invalid_evaluation()
    assert isinstance(report, Mapping)
    complete = _evidence_complete(report)
    qualification = report["mode"] == "qualification"
    fields = _QUALIFICATION_GATES if qualification else _CORRECTNESS_GATES
    values = _gate_values(report)
    thresholds = _run_thresholds(report["performance"])
    gates = {field: _gate_entry(field, values[field], complete, thresholds) for field in fields}
    return _evaluation(complete, gates, qualification)
