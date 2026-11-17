"""Accuracy, percentile and warm-performance arithmetic for the navigation qualification."""

from __future__ import annotations

import math
import sys
import time
from collections.abc import Callable, Iterable, Sequence
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
    GoldQuery,
)

PERFORMANCE_SAMPLES = 20


def _prf_from_counts(true_positive: int, false_positive: int, false_negative: int) -> tuple[float, float, float]:
    actual_count = true_positive + false_positive
    expected_count = true_positive + false_negative
    precision = true_positive / actual_count if actual_count else 1.0
    recall = true_positive / expected_count if expected_count else 1.0
    f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def precision_recall_f1(
    expected: Iterable[object],
    actual: Iterable[object],
) -> dict[str, int | float]:
    """Return set precision/recall/F1 with explicit empty-set semantics."""
    expected_set = set(expected)
    actual_set = set(actual)
    true_positive = len(expected_set & actual_set)
    false_positive = len(actual_set - expected_set)
    false_negative = len(expected_set - actual_set)
    precision, recall, f1 = _prf_from_counts(true_positive, false_positive, false_negative)
    return {
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def _ordered_finite_samples(values: Sequence[float]) -> list[float]:
    ordered = sorted(float(value) for value in values)
    if not all(math.isfinite(value) for value in ordered):
        raise ValueError("percentile samples must be finite")
    return ordered


def nearest_rank_percentile(values: Sequence[float], percentile: float) -> float | None:
    """Return the nearest-rank percentile: sorted[ceil(p*n)-1]."""
    if not 0.0 < percentile <= 1.0 or not math.isfinite(percentile):
        raise ValueError("percentile must be finite and in (0, 1]")
    if not values:
        return None
    ordered = _ordered_finite_samples(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _queries_by_capability(queries: Sequence[GoldQuery]) -> dict[str, tuple[GoldQuery, ...]]:
    return {
        capability: tuple(query for query in queries if query.capability == capability)
        for capability in ("definition", "references", "calls")
    }


def _require_complete_domain(by_capability: dict[str, tuple[GoldQuery, ...]]) -> None:
    expected_counts = {
        "definition": DEFINITION_QUERIES,
        "references": REFERENCE_QUERIES,
        "calls": CALL_QUERIES,
    }
    if any(len(by_capability[capability]) != expected for capability, expected in expected_counts.items()):
        raise ValueError("performance sample requires the complete gold query domain")


def _spread(candidates: tuple[GoldQuery, ...], count: int) -> tuple[GoldQuery, ...]:
    denominator = count - 1
    return tuple(
        candidates[(index * (len(candidates) - 1) + denominator // 2) // denominator]
        for index in range(count)
    )


def _interleaved(definitions, references, calls) -> tuple[GoldQuery, ...]:
    return tuple(
        query
        for index in range(5)
        for query in (definitions[index * 2], references[index], calls[index], definitions[index * 2 + 1])
    )


def _performance_queries(queries: Sequence[GoldQuery]) -> tuple[GoldQuery, ...]:
    by_capability = _queries_by_capability(queries)
    _require_complete_domain(by_capability)
    selected = _interleaved(
        _spread(by_capability["definition"], 10),
        _spread(by_capability["references"], 5),
        _spread(by_capability["calls"], 5),
    )
    if len(selected) != PERFORMANCE_SAMPLES or len({query.query_id for query in selected}) != len(selected):
        raise AssertionError("performance query sample must contain 20 unique queries")
    return selected


def _measure_warm_performance_pair(
    runtime: object,
    request: object,
    *,
    next_deadline: Callable[[], float],
    perf_counter: Callable[[], float] = time.perf_counter,
) -> tuple[tuple[object, ...], tuple[object, ...], float, float]:
    queries = {"direct": runtime.direct_query, "facade": runtime.query}
    results: dict[str, list[object]] = {"direct": [], "facade": []}
    times: dict[str, list[float]] = {"direct": [], "facade": []}
    for operation in ("direct", "facade", "facade", "direct"):
        started = perf_counter()
        results[operation].append(queries[operation](request, deadline=next_deadline()))
        times[operation].append((perf_counter() - started) * 1000.0)
    return (
        tuple(results["direct"]),
        tuple(results["facade"]),
        sum(times["direct"]) / len(times["direct"]),
        sum(times["facade"]) / len(times["facade"]),
    )


def _observed_expected_exception(
    operation: Callable[[], object],
    expected: type[BaseException],
) -> bool:
    try:
        operation()
    except expected:
        return True
    except Exception:
        return False
    return False
