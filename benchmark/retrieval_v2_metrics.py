"""TREC effectiveness metrics, per-query evaluation rows and their per-language aggregation."""
from __future__ import annotations

import math
from collections.abc import Sequence

from retrieval_v2_contract import QueryScope, ScoredCandidate


def recall_at_k(ranked_ids: Sequence[str], relevant_ids: set[str], k: int) -> float:
    if not relevant_ids:
        return 0.0
    return len(set(ranked_ids[:k]) & relevant_ids) / len(relevant_ids)


def all_required_at_k(ranked_ids: Sequence[str], relevant_ids: set[str], k: int) -> float:
    if not relevant_ids:
        return 0.0
    return float(relevant_ids <= set(ranked_ids[:k]))


def parent_recall_at_k(ranked_ids: Sequence[str], relevant_ids: set[str], k: int) -> float:
    return recall_at_k(ranked_ids, relevant_ids, k)


def reciprocal_rank_at_k(ranked_ids: Sequence[str], relevant_ids: set[str], k: int) -> float:
    for rank, candidate_id in enumerate(ranked_ids[:k], 1):
        if candidate_id in relevant_ids:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranked_ids: Sequence[str], gains: dict[str, int], k: int) -> float:
    if not gains:
        return 0.0

    def dcg(values: Sequence[int]) -> float:
        return sum(gain / math.log2(rank + 1) for rank, gain in enumerate(values, 1))

    actual = dcg([gains.get(candidate_id, 0) for candidate_id in ranked_ids[:k]])
    ideal = dcg(sorted(gains.values(), reverse=True)[:k])
    return actual / ideal if ideal else 0.0


def false_answer_rate(traces: Sequence[dict]) -> float | None:
    no_answer = [trace for trace in traces if trace["answerability"] == "unanswerable"]
    if not no_answer:
        return None
    return sum(not trace["abstained"] for trace in no_answer) / len(no_answer)


def macro_average(slices: dict[str, dict], metric: str) -> float | None:
    values = [
        slices[name][metric]
        for name in ("EN", "RU", "ZH", "cross-language")
        if slices[name][metric] is not None
    ]
    return sum(values) / len(values) if values else None


def language_gate_gaps(slices: dict[str, dict], metric: str, gate: float) -> dict[str, float]:
    return {language: slices[language][metric] - gate for language in ("EN", "RU", "ZH")}


def query_scope(query: dict) -> QueryScope:
    return QueryScope(
        projects=tuple(query["project_scope"]),
        temporal_mode=query["temporal_scope"]["mode"],
        as_of=query["temporal_scope"]["as_of"],
    )


def expand_parents(ranked: Sequence[ScoredCandidate]) -> list[str]:
    parents = []
    seen = set()
    for item in ranked:
        if item.parent_id not in seen:
            seen.add(item.parent_id)
            parents.append(item.parent_id)
    return parents


def abstention_contract_valid(query: dict, answer: dict) -> bool:
    if query["answerability"] == "answerable":
        return not answer["abstained"]
    return answer["abstained"] and answer["reason"] == query["allowed_abstention_reason"]


def evaluation_row(query: dict, ranked: Sequence[ScoredCandidate], answer: dict) -> dict:
    ranked_evidence = [item.evidence_id for item in ranked]
    ranked_parents = expand_parents(ranked)
    relevant_evidence = set(query["required_evidence_spans"])
    relevant_parents = set(query["relevant_parents"])
    gains = {item["evidence_id"]: item["gain"] for item in query["graded_evidence"]}
    return {
        "query_id": query["query_id"],
        "language": query["language"],
        "cross_language": query["cross_language"],
        "answerability": query["answerability"],
        "abstained": answer["abstained"],
        "abstention_contract_valid": abstention_contract_valid(query, answer),
        "evidence_recall_at_10": recall_at_k(ranked_evidence, relevant_evidence, 10),
        "evidence_recall_at_20": recall_at_k(ranked_evidence, relevant_evidence, 20),
        "evidence_recall_at_50": recall_at_k(ranked_evidence, relevant_evidence, 50),
        "all_required_evidence_recall_at_20": all_required_at_k(
            ranked_evidence, relevant_evidence, 20
        ),
        "parent_recall_at_10": parent_recall_at_k(ranked_parents, relevant_parents, 10),
        "ndcg_at_10": ndcg_at_k(ranked_evidence, gains, 10),
        "mrr_at_10": reciprocal_rank_at_k(ranked_evidence, relevant_evidence, 10),
    }


def aggregate(rows: Sequence[dict]) -> dict[str, float | int | None]:
    positive = [row for row in rows if row["answerability"] == "answerable"]

    def mean(name: str) -> float | None:
        return sum(row[name] for row in positive) / len(positive) if positive else None

    return {
        "query_count": len(rows),
        "positive_query_count": len(positive),
        "no_answer_query_count": len(rows) - len(positive),
        "evidence_recall_at_10": mean("evidence_recall_at_10"),
        "evidence_recall_at_20": mean("evidence_recall_at_20"),
        "evidence_recall_at_50": mean("evidence_recall_at_50"),
        "all_required_evidence_recall_at_20": mean("all_required_evidence_recall_at_20"),
        "parent_recall_at_10": mean("parent_recall_at_10"),
        "ndcg_at_10": mean("ndcg_at_10"),
        "mrr_at_10": mean("mrr_at_10"),
        "no_answer_false_answer_rate": false_answer_rate(rows),
    }
SHIPPABLE_LICENSES = frozenset({"Apache-2.0", "MIT"})


def _language_rows(rows: Sequence[dict], language: str) -> list[dict]:
    return [row for row in rows if row["language"] == language]


def _cross_language_rows(rows: Sequence[dict]) -> list[dict]:
    return [row for row in rows if row["cross_language"]]


def language_slices(rows: Sequence[dict]) -> dict[str, dict]:
    slices = {language: aggregate(_language_rows(rows, language)) for language in ("EN", "RU", "ZH")}
    slices["cross-language"] = aggregate(_cross_language_rows(rows))
    return slices
