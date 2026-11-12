"""Synthetic retrieval v2 candidate reports for the aggregation and orchestration tests."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.retrieval_v2_helpers import CORPUS, SCHEMA

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import retrieval_v2_contract  # noqa: E402
import retrieval_v2_corpus  # noqa: E402
import retrieval_v2_environment  # noqa: E402
import retrieval_v2_metrics  # noqa: E402


def query_ranking(candidates, query):
    """Deterministic ranking: required evidence first, then evidence id."""
    eligible = retrieval_v2_corpus.filter_candidates(candidates, retrieval_v2_metrics.query_scope(query))
    required = set(query["required_evidence_spans"])
    ordered = sorted(
        eligible, key=lambda item: (item.evidence_id not in required, item.evidence_id)
    )
    top_confidence = 1.0 if query["answerability"] == "answerable" else 0.5
    return [
        retrieval_v2_contract.ScoredCandidate(item, top_confidence - index / 1000)
        for index, item in enumerate(ordered)
    ]


def query_answer(query) -> dict[str, object]:
    if query["answerability"] != "unanswerable":
        return {"abstained": False, "reason": None}
    return {"abstained": True, "reason": query["allowed_abstention_reason"]}


def ranked_evidence(ranked) -> list[dict[str, object]]:
    return [{"evidence_id": item.evidence_id, "score": item.score} for item in ranked]


def reranker_depths(ranked, answer) -> dict[str, object]:
    return {
        "depths": {
            str(depth): {
                "ranked_evidence": ranked_evidence(ranked),
                "duration_ms": float(depth),
                "query_latency_ms": 10.0 + depth,
                "abstained": answer["abstained"],
                "abstention_reason": answer["reason"],
            }
            for depth in (10, 20, 50)
        }
    }


def query_trace(query, ranked, answer, row, reranker) -> dict[str, object]:
    pre_rerank = [item.evidence_id for item in ranked] if reranker else None
    trace = {
        "query_id": query["query_id"],
        "ranked_evidence": ranked_evidence(ranked),
        "ranked_parents": retrieval_v2_metrics.expand_parents(ranked),
        "abstained": answer["abstained"],
        "abstention_reason": answer["reason"],
        "abstention_contract_valid": row["abstention_contract_valid"],
        "latency_ms": 10.0,
        "pre_rerank_candidate_ids": pre_rerank,
        "reranker": None,
    }
    if reranker is not None:
        trace["reranker"] = reranker_depths(ranked, answer)
    return trace


def candidate_rows_and_traces(corpus, candidates, reranker):
    rows = []
    traces = []
    for query in corpus["queries"]:
        ranked = query_ranking(candidates, query)
        answer = query_answer(query)
        row = retrieval_v2_metrics.evaluation_row(query, ranked, answer)
        rows.append(row)
        traces.append(query_trace(query, ranked, answer, row, reranker))
    return rows, traces


def confidence_method(reranker) -> str | None:
    if reranker is None:
        return None
    if reranker["id"] == "BAAI/bge-reranker-v2-m3":
        return "sigmoid_probability_from_sequence_classification_logit"
    return "softmax_probability_of_yes_over_no"


def reranker_block(reranker, overall_metrics, slice_metrics, traces):
    if reranker is None:
        return None
    return {
        "model_id": reranker["id"],
        "revision": reranker["revision"],
        "variant_id": reranker["variant_id"],
        "depths": [10, 20, 50],
        "depth_metrics": {
            str(depth): {
                "overall": overall_metrics,
                "slices": slice_metrics,
                "duration_ms": depth * len(traces),
                "inference_latencies_ms": [float(depth)] * len(traces),
                "warm_latency_p95_ms": 10.0 + depth,
                "shared_resources": [
                    "peak_rss_bytes",
                    "index_size_bytes",
                    "vector_bytes",
                ],
            }
            for depth in (10, 20, 50)
        },
        "formatting": {},
    }


def language_rows(rows, language: str) -> list:
    return [row for row in rows if row["language"] == language]


def cross_language_rows(rows) -> list:
    return [row for row in rows if row["cross_language"]]


def metrics_by_slice(rows) -> dict[str, object]:
    metrics = {
        language: retrieval_v2_metrics.aggregate(language_rows(rows, language))
        for language in ("EN", "RU", "ZH")
    }
    metrics["cross-language"] = retrieval_v2_metrics.aggregate(cross_language_rows(rows))
    return metrics


def candidate_report(selection, spec, *, offset):
    embedding = spec["embedding"]
    reranker = spec["reranker"]
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    candidates = retrieval_v2_corpus.build_candidates(corpus)
    rows, traces = candidate_rows_and_traces(corpus, candidates, reranker)
    overall_metrics = retrieval_v2_metrics.aggregate(rows)
    slice_metrics = metrics_by_slice(rows)
    report = {
        "schema_version": "retrieval-report/v2",
        "corpus_id": "public-synthetic-retrieval-v2",
        "adapter_kind": "model-matrix",
        "quality_claim": True,
        "release_evidence": False,
        "requested_mode": "model-matrix",
        "effective_mode": "model-matrix",
        "fallback_reason": None,
        "model_id": embedding["id"],
        "variant_id": embedding["variant_id"],
        "revision": embedding["revision"],
        "matrix_sha256": selection.matrix_sha256,
        "corpus_sha256": selection.corpus_sha256,
        "benchmark_contract_sha256": retrieval_v2_environment.sha256_json(selection.matrix["benchmark_contract"]),
        "benchmark_runner_sha256": retrieval_v2_environment.runner_sha256(),
        "candidate": spec,
        "methodology": {
            "environment_provenance": retrieval_v2_environment.environment_provenance("numpy-exact"),
            "confidence": {
                "fusion": "RRF divided by its theoretical maximum; bounded to [0,1]",
                "qa_threshold": 0.54,
                "reranker": confidence_method(reranker),
            },
        },
        "macro_average": {
            metric: retrieval_v2_metrics.macro_average(slice_metrics, metric)
            for metric in retrieval_v2_contract.EFFECTIVENESS_FIELDS
        },
        "thresholds": dict(retrieval_v2_contract.THRESHOLDS),
        "gates": {},
        "traces": traces,
        "acquisition_mode": "offline-local-files-only",
        "vector_backend": "numpy-exact",
        "overall": overall_metrics,
        "slices": slice_metrics,
        "measurements": {
            "warm_latency_p95_ms": 100.0 + offset,
            "peak_rss_bytes": 1_000_000_000 + offset,
            "index_size_bytes": 100_000 + offset,
        },
        "reranker": reranker_block(reranker, overall_metrics, slice_metrics, traces),
    }
    return (
        json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()
