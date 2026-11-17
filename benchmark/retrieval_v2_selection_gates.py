"""Lexical tallies, reranker-depth recomputation and the gates a model selection must pass."""
from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path

from retrieval_v2_contract import (
    EFFECTIVENESS_FIELDS,
    LEXICAL_CONFIGURATIONS,
    MODEL_MATRIX_ADAPTER_KIND,
    THRESHOLDS,
    ModelSelection,
    ScoredCandidate,
)
from retrieval_v2_corpus import build_candidates
from retrieval_v2_matrix import load_model_selection
from retrieval_v2_measurements import RETRIEVAL_GATE_METRICS, resource_measurements
from retrieval_v2_metrics import (
    SHIPPABLE_LICENSES,
    abstention_contract_valid,
    expand_parents,
    macro_average,
)
from retrieval_v2_recompute import dict_or_none, metrics_match, recompute_report_metrics

_LEXICAL_COMPARABLE_FIELDS = (
    "schema_version",
    "corpus_sha256",
    "benchmark_contract_sha256",
    "benchmark_runner_sha256",
    "adapter_kind",
    "effective_mode",
)


def _is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _finite_nonnegative(value: object) -> bool:
    return _is_finite_number(value) and value >= 0


def _lexical_configuration_id(report: dict, configurations: dict, seen: dict) -> str:
    configuration = report.get("methodology", {}).get("lexical_configuration")
    lexical = dict_or_none(configuration, "id")
    if lexical not in configurations or lexical in seen:
        raise ValueError("lexical evidence must contain each L0 through L4 exactly once")
    if configuration != configurations[lexical]:
        raise ValueError("lexical evidence configuration differs from the frozen matrix")
    return lexical


def _lexical_evidence_values(report: dict) -> tuple[list, object]:
    overall = report.get("overall", {})
    values = [overall.get(name) for name in RETRIEVAL_GATE_METRICS]
    latency = report.get("measurements", {}).get("warm_latency_p95_ms")
    if not all(_is_finite_number(value) for value in values) or not _finite_nonnegative(latency):
        raise ValueError("lexical ablation evidence is incomplete or nonfinite")
    return values, latency


class _LexicalTally:
    """The L0-L4 ablation reports, checked for comparability as they are added."""

    def __init__(self, configurations: dict) -> None:
        self.configurations = configurations
        self.by_id: dict[str, dict] = {}
        self.provenance: tuple | None = None

    def _require_comparable(self, report: dict) -> None:
        comparable = tuple(report.get(field) for field in _LEXICAL_COMPARABLE_FIELDS)
        if self.provenance is None:
            self.provenance = comparable
            return
        if comparable != self.provenance:
            raise ValueError("lexical ablation evidence is not comparable")

    def add(self, report: dict) -> None:
        lexical = _lexical_configuration_id(report, self.configurations, self.by_id)
        self._require_comparable(report)
        values, latency = _lexical_evidence_values(report)
        self.by_id[lexical] = {
            "id": lexical,
            "quality": sum(float(value) for value in values) / len(values),
            "warm_latency_p95_ms": float(latency),
        }


def select_lexical_winner(
    reports: Sequence[dict], *, configurations: dict | None = None
) -> dict:
    configurations = LEXICAL_CONFIGURATIONS if configurations is None else configurations
    tally = _LexicalTally(configurations)
    for report in reports:
        tally.add(report)
    if set(tally.by_id) != set(configurations):
        raise ValueError("lexical evidence requires the complete L0 through L4 ablation set")
    return min(
        tally.by_id.values(),
        key=lambda item: (-item["quality"], item["warm_latency_p95_ms"], item["id"]),
    )


def _depth_evidence(trace: dict, depth_key: str) -> dict:
    depth_evidence = trace.get("reranker", {}).get("depths", {}).get(depth_key)
    if not isinstance(depth_evidence, dict) or not isinstance(depth_evidence.get("ranked_evidence"), list):
        raise ValueError("reranker trace lacks complete depth rankings")
    return depth_evidence


def _depth_timings(depth_evidence: dict) -> tuple[float, float]:
    duration = depth_evidence.get("duration_ms")
    query_latency = depth_evidence.get("query_latency_ms")
    if not all(_finite_nonnegative(value) for value in (duration, query_latency)):
        raise ValueError("reranker depth trace lacks finite timing")
    return float(duration), float(query_latency)


def _depth_trace(trace: dict, depth_evidence: dict, corpus: dict, candidates: dict) -> dict:
    copied = dict(trace)
    copied["ranked_evidence"] = depth_evidence["ranked_evidence"]
    copied["abstained"] = depth_evidence.get("abstained")
    copied["abstention_reason"] = depth_evidence.get("abstention_reason")
    query = next(item for item in corpus["queries"] if item["query_id"] == copied["query_id"])
    copied["abstention_contract_valid"] = abstention_contract_valid(
        query, {"abstained": copied["abstained"], "reason": copied["abstention_reason"]}
    )
    ranked = [
        ScoredCandidate(candidates[item["evidence_id"]], 0.0)
        for item in copied["ranked_evidence"]
        if item["evidence_id"] in candidates
    ]
    copied["ranked_parents"] = expand_parents(ranked)
    return copied


def _require_depth_timing(claimed: dict, inference_latencies: list[float], query_latencies: list[float]) -> None:
    warm_p95 = resource_measurements(
        query_latencies,
        build_time_ms=0,
        chunk_count=0,
        index_size_bytes=0,
        peak_rss_bytes=None,
        peak_rss_status="shared-run-level",
    )["warm_latency_p95_ms"]
    metrics_match(claimed["inference_latencies_ms"], inference_latencies, "reranker timing")
    metrics_match(claimed["duration_ms"], sum(inference_latencies), "reranker duration")
    metrics_match(claimed["warm_latency_p95_ms"], warm_p95, "reranker warm p95")


def _recomputed_depth(corpus: dict, report: dict, depth: int, candidates: dict) -> tuple[dict, dict]:
    depth_key = str(depth)
    depth_traces = []
    inference_latencies = []
    query_latencies = []
    for trace in report["traces"]:
        depth_evidence = _depth_evidence(trace, depth_key)
        duration, query_latency = _depth_timings(depth_evidence)
        inference_latencies.append(duration)
        query_latencies.append(query_latency)
        depth_traces.append(_depth_trace(trace, depth_evidence, corpus, candidates))
    claimed = report["reranker"]["depth_metrics"][depth_key]
    _require_depth_timing(claimed, inference_latencies, query_latencies)
    synthetic = dict(
        report,
        traces=depth_traces,
        overall=claimed["overall"],
        slices=claimed["slices"],
        macro_average={metric: macro_average(claimed["slices"], metric) for metric in EFFECTIVENESS_FIELDS},
    )
    return recompute_report_metrics(corpus, synthetic)


def recompute_reranker_depth_metrics(corpus: dict, report: dict) -> dict[str, tuple[dict, dict]]:
    if report["reranker"] is None:
        return {}
    candidates = {item.evidence_id: item for item in build_candidates(corpus)}
    return {
        str(depth): _recomputed_depth(corpus, report, depth, candidates)
        for depth in report["reranker"]["depths"]
    }


_GATE_LANGUAGES = ("EN", "RU", "ZH")


def _unit_rate(value: float) -> bool:
    return math.isfinite(value) and 0 <= value <= 1


def _finite_unit_number(value: object) -> bool:
    return _is_finite_number(value) and 0 <= value <= 1


def _language_quality_values(slices: dict) -> list[float]:
    return [float(slices[language][name]) for language in _GATE_LANGUAGES for name in RETRIEVAL_GATE_METRICS]


def _require_quality_rates(overall: dict, slices: dict) -> list[float]:
    quality_values = [float(overall[name]) for name in RETRIEVAL_GATE_METRICS]
    if not all(_unit_rate(value) for value in quality_values + _language_quality_values(slices)):
        raise ValueError("raw report quality metrics must be finite rates")
    return quality_values


def _require_false_answer_rates(overall: dict, slices: dict) -> None:
    values = [
        overall["no_answer_false_answer_rate"],
        *(slices[language]["no_answer_false_answer_rate"] for language in _GATE_LANGUAGES),
    ]
    if not all(_finite_unit_number(value) for value in values):
        raise ValueError("raw report false-answer metrics must be finite rates")


def _selection_language_passes(metrics: dict) -> bool:
    gap = THRESHOLDS["max_language_gate_gap"]
    retrieval = all(float(metrics[name]) >= THRESHOLDS[name] - gap for name in RETRIEVAL_GATE_METRICS)
    return retrieval and metrics["no_answer_false_answer_rate"] <= THRESHOLDS["no_answer_false_answer_rate"]


def _require_resource_values(measurements: dict) -> tuple:
    values = (measurements["warm_latency_p95_ms"], measurements["peak_rss_bytes"], measurements["index_size_bytes"])
    if not all(_finite_nonnegative(value) for value in values):
        raise ValueError("raw report has incomplete or nonfinite resource measurements")
    return values


def _improvement_flags(selection: dict, overall: dict, quality_basis_points: int) -> tuple[bool, bool]:
    baseline = selection["baseline"]
    material = selection["material_improvement"]
    parent_ok = round(float(overall["parent_recall_at_10"]) * 10_000) >= baseline["parent_recall_at_10_basis_points"]
    material_ok = quality_basis_points >= (
        baseline["overall_basis_points"] + material["minimum_absolute_gain_basis_points"]
    )
    return parent_ok, material_ok


def _policy_shippable(item: dict | None):
    if item is None:
        return True
    return (
        item["shipping_eligible"]
        and not item["exclusion_reasons"]
        and item["license"] in SHIPPABLE_LICENSES
        and item["trust_remote_code"] is False
    )


def _selection_objective(quality_basis_points: int, resources: tuple) -> dict:
    p95, rss, index_bytes = resources
    return {
        "index_bytes": int(index_bytes),
        "overall": quality_basis_points,
        "peak_rss_bytes": int(rss),
        "warm_p95_ms": math.ceil(p95),
    }


def selection_report_gates(matrix: dict, report: dict, embedding: dict, reranker: dict | None):
    selection = matrix["selection"]
    overall = report["overall"]
    slices = report["slices"]
    quality_values = _require_quality_rates(overall, slices)
    _require_false_answer_rates(overall, slices)
    quality_basis_points = round(sum(quality_values) / len(quality_values) * 10_000)
    per_language = {language: _selection_language_passes(slices[language]) for language in _GATE_LANGUAGES}
    resources = _require_resource_values(report["measurements"])
    parent_ok, material_ok = _improvement_flags(selection, overall, quality_basis_points)
    shipping = all(_policy_shippable(item) for item in (embedding, reranker))
    false_answer_ok = overall["no_answer_false_answer_rate"] <= THRESHOLDS["no_answer_false_answer_rate"]
    required = {
        "every_language_gate": all(per_language.values()) and false_answer_ok,
        "latency": resources[0] <= selection["limits"]["warm_p95_ms"],
        "license": shipping,
        "no_parent_recall_at_10_regression": parent_ok,
        "ram": resources[1] <= selection["limits"]["peak_rss_bytes"],
    }
    gates = {
        "material_improvement": material_ok,
        "no_parent_recall_at_10_regression": parent_ok,
        "overall": all(required.values()) and material_ok,
        "per_language": per_language,
        "required": required,
        "shipping_eligible": shipping,
    }
    return gates, _selection_objective(quality_basis_points, resources)


def objective_dominates(left: dict, right: dict, objectives: dict) -> bool:
    weak = []
    strict = []
    for name, direction in objectives.items():
        if direction == "maximize":
            weak.append(left[name] >= right[name])
            strict.append(left[name] > right[name])
        else:
            weak.append(left[name] <= right[name])
            strict.append(left[name] < right[name])
    return all(weak) and any(strict)


CANDIDATE_REPORT_EQUALITIES = (
    ("schema_version", "retrieval-report/v2"),
    ("adapter_kind", MODEL_MATRIX_ADAPTER_KIND),
    ("effective_mode", MODEL_MATRIX_ADAPTER_KIND),
    ("acquisition_mode", "offline-local-files-only"),
    ("thresholds", THRESHOLDS),
)
CANDIDATE_REPORT_IDENTITIES = (("release_evidence", False), ("fallback_reason", None))
RERANKER_DEPTH_KEYS = frozenset({"10", "20", "50"})


def fields_equal(report: dict, pairs) -> bool:
    return all(report[field] == expected for field, expected in pairs)


def fields_identical(report: dict, pairs) -> bool:
    return all(report[field] is expected for field, expected in pairs)


def first_candidate_selection(matrix: dict, matrix_path: Path | str, corpus_path: Path | str) -> ModelSelection:
    first = matrix["embeddings"][0]
    return load_model_selection(
        matrix_path,
        corpus_path,
        model_id=first["id"],
        variant_id=first["variants"][0]["variant_id"],
    )
