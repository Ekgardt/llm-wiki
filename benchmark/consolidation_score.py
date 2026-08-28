"""Paired scoring for the consolidation stand (MEM-13).

MEM-10 measured one arm. This module scores two arms of the *same* question —
one answered from a vault whose daily log carries only the raw captured
sessions, one from the same vault after `episode_consolidation.consolidate_day`
has appended its durable-item entries — and reports the difference as a paired
comparison rather than two independent accuracies.

Why paired and not two runs: this vault already recorded (log, 2026-08-26) that
its ten-question stands wander by one case between runs of identical code,
because the optional retrieval legs are deadline-bounded and drop out under
load. Comparing two independent means at n=12 would report that wander. Pairing
removes every per-question difficulty term, and the only statistic that then
matters is the discordant pairs: questions one arm got right and the other got
wrong. `mcnemar_exact` is the exact binomial test on exactly those, so a
reported difference either survives the coin-flip null or is named as noise.

Correctness for a single arm is `longmemeval_score.score_question` unchanged —
same normalisation, same gold-alias handling, same abstention rule — so an arm
of this stand is directly comparable to the MEM-10 rows.
"""
from __future__ import annotations

import sys
from math import comb
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_score  # noqa: E402

ARMS = ("baseline", "consolidated")
# A stand that broke is not a memory system that answered wrongly. These reach
# the row when the worker or one arm raised — an activation refusal, a dead
# subprocess — and they are dropped from the comparison exactly as a provider
# failure is, for the same reason: nothing was produced to compare.
HARNESS_ERROR_KINDS = frozenset({"harness_failure", "arm_failure"})


def is_ungraded(row: dict) -> bool:
    if longmemeval_score.is_provider_failure(row):
        return True
    return row.get("error_kind") in HARNESS_ERROR_KINDS


def mcnemar_exact(discordant_baseline: int, discordant_consolidated: int) -> float:
    """Two-sided exact binomial p over the discordant pairs.

    Under the null "consolidation changes nothing", each question that the two
    arms disagree on is a fair coin. With no disagreements at all there is no
    evidence of a difference, which is p = 1.0 rather than an error.
    """
    total = discordant_baseline + discordant_consolidated
    if total == 0:
        return 1.0
    smaller = min(discordant_baseline, discordant_consolidated)
    tail = sum(comb(total, index) for index in range(smaller + 1))
    return min(1.0, 2.0 * tail / (2**total))


def arm_row(paired: dict, arm: str) -> dict:
    """One arm of a paired record, in the flat MEM-10 row shape.

    The judge (`longmemeval_judge.py`) and `longmemeval_score.aggregate` both
    read that shape, so an arm can be judged and reported with no new code.
    """
    row = dict(paired.get(arm) or {})
    for key in ("question_id", "question_type", "category", "is_abstention", "question", "gold"):
        row.setdefault(key, paired.get(key))
    row["arm"] = arm
    return row


def _scored_arm(paired: dict, arm: str) -> dict:
    return longmemeval_score.score_question(arm_row(paired, arm))


def _both_graded(paired: dict) -> bool:
    """A pair counts only when both arms produced something to grade.

    A provider failure on one arm is not evidence about consolidation; keeping
    the pair would credit or blame it for this machine's CLI.
    """
    rows = [arm_row(paired, arm) for arm in ARMS]
    return not any(is_ungraded(row) for row in rows)


def _verdicts(paired: dict) -> tuple[bool, bool]:
    baseline, consolidated = (_scored_arm(paired, arm) for arm in ARMS)
    return bool(baseline.get("correct")), bool(consolidated.get("correct"))


def _discordance(pairs: list[dict]) -> tuple[int, int]:
    """(baseline-only right, consolidated-only right) over graded pairs."""
    baseline_only = 0
    consolidated_only = 0
    for paired in pairs:
        baseline, consolidated = _verdicts(paired)
        baseline_only += int(baseline and not consolidated)
        consolidated_only += int(consolidated and not baseline)
    return baseline_only, consolidated_only


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _arm_values(pairs: list[dict], arm: str, key: str) -> list[float]:
    rows = [arm_row(paired, arm) for paired in pairs]
    return [float(row[key]) for row in rows if row.get(key) is not None]


def _present_numbers(rows: list[dict], key: str) -> list[float]:
    return [float(row[key]) for row in rows if row.get(key) is not None]


def _status_count(rows: list[dict], status: str) -> int:
    return len([row for row in rows if row.get("status") == status])


_ARM_MEAN_KEYS = (
    ("mean_est_prompt_tokens", "est_prompt_tokens"),
    ("mean_retrieve_seconds", "retrieve_seconds"),
    ("mean_answer_seconds", "answer_seconds"),
    ("mean_chunks", "chunks"),
    ("mean_retrieved", "retrieved"),
    ("mean_retrieved_from_consolidation", "retrieved_from_consolidation"),
)


def _arm_summary(pairs: list[dict], arm: str) -> dict:
    scored = [_scored_arm(paired, arm) for paired in pairs]
    summary = {
        "n": len(scored),
        "accuracy": _mean([float(bool(row.get("correct"))) for row in scored]),
        "f1": _mean(_present_numbers(scored, "f1")),
        "answered": _status_count(scored, "answered"),
        "insufficient_evidence": _status_count(scored, "insufficient_evidence"),
    }
    for name, key in _ARM_MEAN_KEYS:
        summary[name] = _mean(_arm_values(pairs, arm, key))
    return summary


_COST_MEAN_KEYS = (
    ("mean_provider_calls", "provider_calls"),
    ("mean_seconds", "seconds"),
    ("mean_prompt_chars", "prompt_chars"),
    ("mean_items", "items"),
    ("mean_days", "days"),
)


def _consolidation_cost(pairs: list[dict]) -> dict:
    costs = [dict(paired.get("consolidation") or {}) for paired in pairs]
    return {
        name: _mean(_present_numbers(costs, key)) for name, key in _COST_MEAN_KEYS
    }


def _difference(pairs: list[dict]) -> dict:
    baseline_only, consolidated_only = _discordance(pairs)
    summaries = {arm: _arm_summary(pairs, arm) for arm in ARMS}
    accuracies = [summaries[arm]["accuracy"] or 0.0 for arm in ARMS]
    return {
        "graded_pairs": len(pairs),
        "baseline_only_correct": baseline_only,
        "consolidated_only_correct": consolidated_only,
        "both_or_neither": len(pairs) - baseline_only - consolidated_only,
        "accuracy_delta": round(accuracies[1] - accuracies[0], 4),
        "mcnemar_exact_p": round(mcnemar_exact(baseline_only, consolidated_only), 4),
    }


def paired_report(paired_rows: list[dict]) -> dict:
    """The whole stand: per-arm numbers, the paired difference, and its cost."""
    pairs = [row for row in paired_rows if _both_graded(row)]
    report = {
        "pairs_attempted": len(paired_rows),
        "pairs_graded": len(pairs),
        "pairs_dropped_ungraded": len(paired_rows) - len(pairs),
        "baseline": _arm_summary(pairs, "baseline"),
        "consolidated": _arm_summary(pairs, "consolidated"),
        "consolidation_cost": _consolidation_cost(pairs),
    }
    report["difference"] = _difference(pairs)
    return report
