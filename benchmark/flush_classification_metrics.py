"""Pure scoring and gates for session-classification benchmarks."""
from __future__ import annotations

from dataclasses import dataclass

from llm_wiki.classification_protocol import classify_response


@dataclass(frozen=True)
class CaseOutcome:
    case_id: str
    expected_tier: str
    observed_tier: str
    tier_matched: bool
    missing_markers: tuple[str, ...]
    falsely_promoted: bool


def is_confirmed(case: dict) -> bool:
    """A hand-written v1 case is confirmed; a built case says so itself."""
    return case.get("label_status", "confirmed") == "confirmed"


def confirmed_cases(corpus: dict) -> list[dict]:
    return [case for case in corpus["cases"] if is_confirmed(case)]


def label_state(corpus: dict) -> dict[str, object]:
    """How many labels the metrics rest on, and how many they leave out.

    A corpus built from real sessions is labelled by two automatic readings
    (`build_flush_corpus.py`); a case they disagree on is contested, stays in
    the corpus, and counts in no metric — the same rule the product applies to
    a claim its evaluators disagree on. Nobody reviews labels by hand.
    """
    cases = corpus["cases"]
    confirmed = len(confirmed_cases(corpus))
    return {
        "case_count": len(cases),
        "confirmed_count": confirmed,
        "contested_count": len(cases) - confirmed,
    }


def score_case(case: dict, response: str) -> CaseOutcome:
    """Compare one classification against its label."""
    tier, body = classify_response(response)
    expected = str(case["expected_tier"])
    missing = tuple(
        marker for marker in case["required_markers"] if marker not in body
    )
    return CaseOutcome(
        case_id=str(case["case_id"]),
        expected_tier=expected,
        observed_tier=tier,
        tier_matched=tier == expected,
        missing_markers=missing,
        falsely_promoted=expected == "ok" and tier != "ok",
    )


def _rate(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 1.0
    return round(numerator / denominator, 6)


def _durable_cases(outcomes: list[CaseOutcome]) -> list[CaseOutcome]:
    return [item for item in outcomes if item.expected_tier != "ok"]


def _abstaining_cases(outcomes: list[CaseOutcome]) -> list[CaseOutcome]:
    return [item for item in outcomes if item.expected_tier == "ok"]


def _false_promotion_rate(outcomes: list[CaseOutcome]) -> float:
    abstaining = _abstaining_cases(outcomes)
    if not abstaining:
        return 0.0
    promoted = [item for item in abstaining if item.falsely_promoted]
    return _rate(len(promoted), len(abstaining))


def measure(outcomes: list[CaseOutcome]) -> dict[str, float]:
    durable = _durable_cases(outcomes)
    kept = [item for item in durable if not item.missing_markers]
    matched = [item for item in outcomes if item.tier_matched]
    return {
        "case_count": len(outcomes),
        "durable_case_count": len(durable),
        "tier_accuracy": _rate(len(matched), len(outcomes)),
        "durable_content_recall": _rate(len(kept), len(durable)),
        "false_promotion_rate": _false_promotion_rate(outcomes),
    }


def evaluate(metrics: dict[str, float], thresholds: dict[str, float]) -> dict[str, object]:
    results = {
        "tier_accuracy": metrics["tier_accuracy"] >= thresholds["tier_accuracy"],
        "durable_content_recall": (
            metrics["durable_content_recall"] >= thresholds["durable_content_recall"]
        ),
        "false_promotion_rate": (
            metrics["false_promotion_rate"] <= thresholds["false_promotion_rate"]
        ),
    }
    results["labelled"] = metrics["case_count"] > 0
    return {"metric_results": results, "passed": all(results.values())}
