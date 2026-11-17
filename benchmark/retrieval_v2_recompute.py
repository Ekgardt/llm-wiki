"""Recompute report metrics from their traces and verify the bound baseline evidence."""
from __future__ import annotations

import json
import math
from collections.abc import Sequence
from pathlib import Path

import retrieval_v2_corpus
from retrieval_v2_contract import (
    DEFAULT_SCHEMA,
    EFFECTIVENESS_FIELDS,
    FROZEN_CORPUS_IDS,
    LEXICAL_CONFIGURATIONS,
    MODEL_MATRIX_ADAPTER_KIND,
    REPORT_FIELDS,
    ROOT,
    THRESHOLDS,
    ScoredCandidate,
)
from retrieval_v2_corpus import build_candidates, filter_candidates
from retrieval_v2_environment import (
    is_recorded_digest,
    locked_package_version_sets,
    observed_runtime_environment,
    sha256_json,
)
from retrieval_v2_measurements import RETRIEVAL_GATE_METRICS
from retrieval_v2_metrics import (
    aggregate,
    evaluation_row,
    expand_parents,
    language_slices,
    macro_average,
    query_scope,
)


def candidate_key(candidate: dict) -> str:
    return json.dumps(candidate, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _variant_matches(variant: dict, target: dict) -> bool:
    return variant["variant_id"] == target["variant_id"] and variant["dimensions"] == target["dimensions"]


def _candidate_matches(item: dict, target: dict) -> bool:
    if item["id"] != target["id"] or item["revision"] != target["revision"]:
        return False
    return any(_variant_matches(variant, target) for variant in item["variants"])


def _matching_candidate(items: Sequence[dict], target: dict) -> dict | None:
    return next((item for item in items if _candidate_matches(item, target)), None)


def _selected_reranker_policy(matrix: dict, reranker_target: dict | None) -> dict | None:
    if reranker_target is None:
        return None
    reranker = _matching_candidate(matrix["rerankers"], reranker_target)
    if reranker is None:
        raise ValueError("raw report selects an unknown reranker target")
    return reranker


def candidate_policy(matrix: dict, spec: dict) -> tuple[dict, dict | None]:
    embedding = _matching_candidate(matrix["embeddings"], spec["embedding"])
    if embedding is None:
        raise ValueError("raw report selects an unknown embedding target")
    return embedding, _selected_reranker_policy(matrix, spec["reranker"])


def _dict_metrics_match(claimed: object, recomputed: dict, path: str) -> None:
    if not isinstance(claimed, dict) or set(claimed) != set(recomputed):
        raise ValueError(f"candidate metric object mismatch at {path}")
    for key, value in recomputed.items():
        metrics_match(claimed[key], value, f"{path}.{key}")


def _list_metrics_match(claimed: object, recomputed: list, path: str) -> None:
    if not isinstance(claimed, list) or len(claimed) != len(recomputed):
        raise ValueError(f"candidate metric list mismatch at {path}")
    for index, value in enumerate(recomputed):
        metrics_match(claimed[index], value, f"{path}[{index}]")


def _exact_metric_match(claimed: object, recomputed: object, path: str) -> None:
    if claimed != recomputed or type(claimed) is not type(recomputed):
        raise ValueError(f"candidate metric mismatch at {path}")


def _float_metric_match(claimed: object, recomputed: object, path: str) -> None:
    if not isinstance(claimed, (int, float)) or not math.isclose(
        float(claimed), float(recomputed), rel_tol=0.0, abs_tol=1e-12
    ):
        raise ValueError(f"candidate metric mismatch at {path}")


def _metric_matcher(recomputed: object):
    if isinstance(recomputed, dict):
        return _dict_metrics_match
    if isinstance(recomputed, list):
        return _list_metrics_match
    return _scalar_metric_matcher(recomputed)


def _scalar_metric_matcher(recomputed: object):
    if recomputed is None or isinstance(recomputed, int):
        return _exact_metric_match
    return _float_metric_match


def metrics_match(claimed: object, recomputed: object, path: str) -> None:
    _metric_matcher(recomputed)(claimed, recomputed, path)


_GOLD_FIELDS = frozenset(
    {
        "answerability",
        "allowed_abstention_reason",
        "cross_language",
        "graded_evidence",
        "language",
        "negative_candidates",
        "project_scope",
        "relevant_parents",
        "required_evidence_spans",
        "temporal_scope",
    }
)
_APPROVED_RERANKER_SOURCES = frozenset(
    {
        None,
        "sigmoid_probability_from_sequence_classification_logit",
        "softmax_probability_of_yes_over_no",
    }
)
_RANKED_ITEM_KEYS = frozenset({"evidence_id", "score"})


def _dict_contains_gold(value: dict) -> bool:
    if _GOLD_FIELDS & set(value):
        return True
    return any(_contains_gold(item) for item in value.values())


def _contains_gold(value: object) -> bool:
    if isinstance(value, dict):
        return _dict_contains_gold(value)
    if isinstance(value, list):
        return any(_contains_gold(item) for item in value)
    return False


def _require_trace_count(traces: object, queries: dict) -> None:
    if not isinstance(traces, list) or len(traces) != len(queries):
        raise ValueError("candidate traces must contain every frozen query exactly once")


def _require_trace_ids(traces: list, queries: dict) -> None:
    trace_ids = [trace.get("query_id") for trace in traces if isinstance(trace, dict)]
    if len(trace_ids) != len(traces) or set(trace_ids) != set(queries):
        raise ValueError("candidate traces contain missing, extra, or duplicate query IDs")


def _require_approved_confidence(report: dict) -> None:
    confidence = report["methodology"].get("confidence")
    if not isinstance(confidence, dict) or confidence.get("fusion") != (
        "RRF divided by its theoretical maximum; bounded to [0,1]"
    ) or confidence.get("qa_threshold") != 0.54:
        raise ValueError("candidate report confidence source or calibration is not approved")
    if confidence.get("reranker") not in _APPROVED_RERANKER_SOURCES:
        raise ValueError("candidate report reranker confidence source is not approved")


def _malformed_ranked_item(item: object) -> bool:
    return not isinstance(item, dict) or set(item) != _RANKED_ITEM_KEYS


def _well_formed_ranked_evidence(trace: dict) -> list:
    ranked_evidence = trace.get("ranked_evidence")
    if not isinstance(ranked_evidence, list) or any(_malformed_ranked_item(item) for item in ranked_evidence):
        raise ValueError("candidate trace ranked evidence is malformed")
    return ranked_evidence


def _ids_match_eligible(ranked_ids: list[str], eligible: set[str], require_complete: bool) -> bool:
    if require_complete:
        return set(ranked_ids) == eligible
    return set(ranked_ids) <= eligible


def _require_exact_eligible_ids(ranked_ids: list[str], eligible: set[str], require_complete: bool) -> None:
    if len(ranked_ids) != len(set(ranked_ids)) or not _ids_match_eligible(ranked_ids, eligible, require_complete):
        raise ValueError("candidate trace does not contain exact eligible candidate IDs")


def _normalized_score(score: object) -> bool:
    return isinstance(score, (int, float)) and math.isfinite(float(score)) and 0 <= float(score) <= 1


def _require_normalized_scores(ranked_evidence: list) -> None:
    if any(not _normalized_score(item["score"]) for item in ranked_evidence):
        raise ValueError("candidate trace scores must be finite normalized confidence")


def _trace_answer(trace: dict) -> dict:
    answer = {"abstained": trace.get("abstained"), "reason": trace.get("abstention_reason")}
    if type(answer["abstained"]) is not bool:
        raise ValueError("candidate trace abstention is invalid")
    return answer


def _calibrated_reason(expected_abstained: bool) -> str | None:
    if expected_abstained:
        return "not-in-corpus"
    return None


def _require_calibrated_abstention(answer: dict, ranked_evidence: list) -> None:
    expected_abstained = not ranked_evidence or ranked_evidence[0]["score"] < 0.54
    if answer["abstained"] is not expected_abstained:
        raise ValueError("candidate trace abstention differs from normalized confidence")
    if answer["reason"] != _calibrated_reason(expected_abstained):
        raise ValueError("candidate trace abstention reason differs from calibration")


class _TraceRecompute:
    """Recomputes one report's evaluation rows from its traces and the frozen corpus."""

    def __init__(self, corpus: dict, *, require_complete_rankings: bool, require_normalized_confidence: bool) -> None:
        self.candidates = build_candidates(corpus)
        self.by_id = {candidate.evidence_id: candidate for candidate in self.candidates}
        self.require_complete_rankings = require_complete_rankings
        self.require_normalized_confidence = require_normalized_confidence

    def _eligible_ids(self, query: dict) -> set[str]:
        return {item.evidence_id for item in filter_candidates(self.candidates, query_scope(query))}

    def _checked_ranking(self, trace: dict, query: dict) -> tuple[list, list[ScoredCandidate]]:
        ranked_evidence = _well_formed_ranked_evidence(trace)
        ranked_ids = [item["evidence_id"] for item in ranked_evidence]
        _require_exact_eligible_ids(ranked_ids, self._eligible_ids(query), self.require_complete_rankings)
        if self.require_normalized_confidence:
            _require_normalized_scores(ranked_evidence)
        ranked = [ScoredCandidate(self.by_id[evidence_id], 0.0) for evidence_id in ranked_ids]
        if trace.get("ranked_parents") != expand_parents(ranked):
            raise ValueError("candidate trace parent expansion mismatch")
        return ranked_evidence, ranked

    def row(self, trace: dict, query: dict) -> dict:
        ranked_evidence, ranked = self._checked_ranking(trace, query)
        answer = _trace_answer(trace)
        if self.require_normalized_confidence:
            _require_calibrated_abstention(answer, ranked_evidence)
        row = evaluation_row(query, ranked, answer)
        if trace.get("abstention_contract_valid") is not row["abstention_contract_valid"]:
            raise ValueError("candidate trace abstention contract mismatch")
        return row


def recompute_report_metrics(
    corpus: dict,
    report: dict,
    *,
    require_complete_rankings: bool = True,
    require_normalized_confidence: bool = True,
) -> tuple[dict, dict]:
    queries = {query["query_id"]: query for query in corpus["queries"]}
    traces = report["traces"]
    _require_trace_count(traces, queries)
    _require_trace_ids(traces, queries)
    if _contains_gold(report):
        raise ValueError("candidate report contains forbidden gold fields")
    _require_approved_confidence(report)
    recompute = _TraceRecompute(
        corpus,
        require_complete_rankings=require_complete_rankings,
        require_normalized_confidence=require_normalized_confidence,
    )
    rows = [recompute.row(trace, queries[trace["query_id"]]) for trace in traces]
    overall = aggregate(rows)
    slices = language_slices(rows)
    metrics_match(report["overall"], overall, "overall")
    metrics_match(report["slices"], slices, "slices")
    macro = {metric: macro_average(slices, metric) for metric in EFFECTIVENESS_FIELDS}
    metrics_match(report["macro_average"], macro, "macro_average")
    return overall, slices


def baseline_policy_sha256(report: dict) -> str:
    return sha256_json(
        {
            "acquisition_mode": report.get("acquisition_mode"),
            "artifact_kind": "retrieval-baseline-policy",
            "benchmark_contract_sha256": report.get("benchmark_contract_sha256"),
            "benchmark_runner_sha256": report.get("benchmark_runner_sha256"),
            "candidate": report.get("candidate"),
            "corpus_sha256": report.get("corpus_sha256"),
            "environment_provenance": report.get("methodology", {}).get(
                "environment_provenance"
            ),
            "lexical_configuration": report.get("methodology", {}).get(
                "lexical_configuration"
            ),
            "matrix_sha256": report.get("matrix_sha256"),
            "schema_version": 1,
            "thresholds_basis_points": {
                name: round(float(value) * 10_000)
                for name, value in (report.get("thresholds") or {}).items()
            },
            "vector_backend": report.get("vector_backend"),
        }
    )


_BASELINE_ENVIRONMENT_KEYS = frozenset({"packages", "runner_sha256", "uv_lock_sha256", "verified_lock"})
_VERIFIED_LOCK_KEYS = frozenset({"packages", "package_map_sha256", "uv_lock_sha256"})
_BASELINE_REQUIRED_PACKAGES = frozenset({"jieba", "numpy", "sentence-transformers", "torch", "transformers"})
_ENVIRONMENT_PACKAGE_NAMES = ("numpy", "sentence-transformers", "torch", "transformers")


def dict_or_none(value: object, key: str) -> object:
    if isinstance(value, dict):
        return value.get(key)
    return None


def _keys_are(value: object, keys: frozenset) -> bool:
    return isinstance(value, dict) and set(value) == keys


def _expected_environment_packages(frozen_packages: object) -> dict:
    packages = {name: dict_or_none(frozen_packages, name) for name in _ENVIRONMENT_PACKAGE_NAMES}
    packages["usearch"] = None
    return packages


def _environment_shape_valid(environment: object, verified: object, frozen_packages: object) -> bool:
    return (
        _keys_are(environment, _BASELINE_ENVIRONMENT_KEYS)
        and _keys_are(verified, _VERIFIED_LOCK_KEYS)
        and _keys_are(frozen_packages, _BASELINE_REQUIRED_PACKAGES)
    )


def _frozen_versions_locked(frozen_packages: dict, locked: dict[str, set[str]]) -> bool:
    return not any(
        not isinstance(version, str) or version not in locked.get(name, set())
        for name, version in frozen_packages.items()
    )


def _environment_bindings_valid(report: dict, environment: dict, verified: dict, frozen_packages: dict) -> bool:
    if environment["packages"] != _expected_environment_packages(frozen_packages):
        return False
    if environment["runner_sha256"] != report.get("benchmark_runner_sha256"):
        return False
    return _lock_bindings_valid(environment, verified, frozen_packages)


def _lock_bindings_valid(environment: dict, verified: dict, frozen_packages: dict) -> bool:
    if not is_recorded_digest(environment["uv_lock_sha256"]):
        return False
    if verified["uv_lock_sha256"] != environment["uv_lock_sha256"]:
        return False
    return verified["package_map_sha256"] == sha256_json(frozen_packages)


def _baseline_environment_valid(report: dict, environment, verified, frozen_packages, locked) -> bool:
    if not _environment_shape_valid(environment, verified, frozen_packages):
        return False
    if not _frozen_versions_locked(frozen_packages, locked):
        return False
    return _environment_bindings_valid(report, environment, verified, frozen_packages)


def _verify_baseline_package_contract(report: dict) -> dict:
    """Check the baseline was measured in the environment it claims.

    The binding is to the packages this benchmark loads, not to the byte digest
    of `uv.lock`. That file locks several hundred packages; retiring the Cognee
    bridge in `350eec8` regenerated it and invalidated a frozen baseline whose
    every relevant version was still locked exactly as recorded. A contract
    that fails on unrelated dependency churn stops testing the environment and
    starts testing the file.

    What is still bound, and is what "the environment is unchanged" means here:
    the exact package set, each frozen version still present in the current
    `uv.lock`, the report's own package map digest, and the report's internal
    consistency. Approved by the machine owner on 2026-08-17; see
    `knowledge/notes/baseline-environment-binding-decision.md`.
    """
    environment = report.get("methodology", {}).get("environment_provenance")
    verified = dict_or_none(environment, "verified_lock")
    locked = locked_package_version_sets(ROOT / "uv.lock")
    frozen_packages = dict_or_none(verified, "packages")
    if not _baseline_environment_valid(report, environment, verified, frozen_packages, locked):
        raise ValueError("bound baseline frozen package contract mismatch")
    lexical_config = report.get("methodology", {}).get("lexical_configuration", {}).get("id")
    return observed_runtime_environment(report.get("vector_backend"), lexical_config=lexical_config)


_BASELINE_TARGET = {
    "embedding": {
        "dimensions": 384,
        "id": "BAAI/bge-small-en-v1.5",
        "revision": "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a",
        "variant_id": "float32-384d",
    },
    "reranker": None,
}
_BASELINE_EQUALITIES = (
    ("schema_version", "retrieval-report/v2"),
    ("adapter_kind", MODEL_MATRIX_ADAPTER_KIND),
    ("requested_mode", MODEL_MATRIX_ADAPTER_KIND),
    ("candidate", _BASELINE_TARGET),
    ("effective_mode", MODEL_MATRIX_ADAPTER_KIND),
    ("acquisition_mode", "offline-local-files-only"),
    ("vector_backend", "numpy-exact"),
    ("thresholds", THRESHOLDS),
)
_BASELINE_IDENTITIES = (("quality_claim", True), ("fallback_reason", None), ("release_evidence", False))


def _baseline_values_match(report: dict) -> bool:
    equal = all(report.get(field) == expected for field, expected in _BASELINE_EQUALITIES)
    return equal and all(report.get(field) is expected for field, expected in _BASELINE_IDENTITIES)


def _baseline_bound_to_matrix(matrix: dict, report: dict) -> bool:
    if report.get("corpus_sha256") != matrix["benchmark_contract"]["corpus"]["sha256"]:
        return False
    if report.get("benchmark_contract_sha256") != sha256_json(matrix["benchmark_contract"]):
        return False
    return report.get("methodology", {}).get("lexical_configuration") == LEXICAL_CONFIGURATIONS["L4"]


def _baseline_report_comparable(matrix: dict, report: dict) -> bool:
    if set(report) != REPORT_FIELDS or report.get("corpus_id") not in FROZEN_CORPUS_IDS:
        return False
    if not _baseline_values_match(report):
        return False
    return _baseline_bound_to_matrix(matrix, report)


def _quality_basis_points(overall: dict) -> int:
    return round(sum(float(overall[name]) for name in RETRIEVAL_GATE_METRICS) / 4 * 10_000)


def _require_baseline_matches(matrix: dict, overall: dict) -> None:
    quality = _quality_basis_points(overall)
    baseline = matrix["selection"]["baseline"]
    if baseline["overall_basis_points"] != quality or baseline[
        "parent_recall_at_10_basis_points"
    ] != round(float(overall["parent_recall_at_10"]) * 10_000):
        raise ValueError("selection baseline metrics do not match bound raw evidence")


def verified_baseline_metrics(matrix: dict, report: dict, *, corpus_path: Path | str):
    if matrix["selection"]["baseline"].get("policy_sha256") != baseline_policy_sha256(report):
        raise ValueError("bound baseline policy fingerprint mismatch")
    _verify_baseline_package_contract(report)
    if not _baseline_report_comparable(matrix, report):
        raise ValueError("bound baseline is not comparable retrieval-v2 BGE-small evidence")
    corpus = retrieval_v2_corpus.load_corpus(corpus_path, Path(corpus_path).with_name(DEFAULT_SCHEMA.name))
    overall, slices = recompute_report_metrics(corpus, report)
    _require_baseline_matches(matrix, overall)
    return overall, slices
