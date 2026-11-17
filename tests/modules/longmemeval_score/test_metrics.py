"""Original deterministic metric assertions, unchanged."""
from __future__ import annotations

import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parents[3] / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_score  # noqa: E402


def test_exact_match_ignores_articles_case_and_punctuation() -> None:
    assert longmemeval_score.exact_match("The Blue Bike!", "blue bike")


def test_contains_finds_the_gold_answer_inside_a_sentence() -> None:
    hit = longmemeval_score.contains_answer(
        "Business Administration", "I graduated with a Business Administration degree."
    )

    assert hit
    assert not longmemeval_score.contains_answer("85 dollars", "the sneakers were red")


def test_token_f1_rewards_overlap_and_nothing_else() -> None:
    assert longmemeval_score.token_f1("red sneakers", "red sneakers") == 1.0
    assert longmemeval_score.token_f1("red sneakers", "blue coat") == 0.0


def test_an_abstention_question_scores_by_abstaining() -> None:
    abstained = longmemeval_score.score_question(
        {"is_abstention": True, "status": "insufficient_evidence", "error": None}
    )
    answered = longmemeval_score.score_question(
        {"is_abstention": True, "status": "answered", "error": None, "hypothesis": "x"}
    )

    assert abstained["correct"] is True
    assert answered["correct"] is False


def test_a_provider_failure_is_not_a_correct_abstention() -> None:
    failed = longmemeval_score.score_question(
        {
            "is_abstention": True,
            "status": "error",
            "error": "GroundedQAError: grounded QA provider returned no response",
            "error_kind": "provider_no_response",
        }
    )

    assert failed["correct"] is False


def _aggregated_report() -> dict:
    rows = [
        {
            "question_id": "a",
            "category": "multi-session",
            "is_abstention": False,
            "gold": "85 dollars",
            "hypothesis": "You paid 85 dollars for the sneakers",
            "status": "answered",
            "est_prompt_tokens": 1500,
            "est_total_prompt_tokens": 1720,
            "judge_correct": True,
            "retrieve_seconds": 2.0,
            "answer_seconds": 50.0,
        },
        {
            "question_id": "b",
            "category": "multi-session",
            "is_abstention": False,
            "gold": "a red bike",
            "hypothesis": "",
            "status": "error",
            "error_kind": "provider_no_response",
        },
    ]
    return longmemeval_score.aggregate(rows)


def test_a_gold_alias_introduced_by_or_matches_on_its_own() -> None:
    """`25 minutes and 50 seconds (or 25:50)` is answerable as `25:50`.

    Measured on question 6a1eabeb of the 2026-08-28 run: the product answered
    `The user's personal best time in the charity 5K run was 25:50`, which is
    right, and the deterministic metric called it wrong because the alias
    variant still carried the word that introduces it.
    """
    gold = "25 minutes and 50 seconds (or 25:50)"

    assert "25 50" in longmemeval_score.gold_variants(gold)
    assert longmemeval_score.contains_answer(
        gold, "The user's personal best time in the charity 5K run was 25:50."
    )


def test_a_plain_parenthetical_alias_still_matches() -> None:
    gold = "University of California, Los Angeles (UCLA)"

    assert longmemeval_score.contains_answer(gold, "She studied at UCLA.")
    assert not longmemeval_score.contains_answer(gold, "She studied at MIT.")


def test_aggregate_holds_provider_failures_out_of_accuracy() -> None:
    """A question the provider never answered is not a wrong answer.

    Both rows are attempts, but only one produced anything to grade, so the
    denominator of accuracy is `scored`, not `n`. Counting the failure as
    wrong would publish this machine's single-provider throughput as if it
    were the memory system's recall.
    """
    report = _aggregated_report()

    assert report["multi-session"]["n"] == 2
    assert report["multi-session"]["scored"] == 1
    assert report["multi-session"]["accuracy"] == 1.0
    assert report["multi-session"]["provider_failures"] == 1


def test_a_category_of_nothing_but_provider_failures_reports_no_accuracy() -> None:
    report = longmemeval_score.aggregate(
        [
            {
                "question_id": "z",
                "category": "temporal-reasoning",
                "is_abstention": False,
                "gold": "March",
                "hypothesis": "",
                "status": "error",
                "error_kind": "provider_deadline",
            }
        ]
    )

    assert report["temporal-reasoning"]["scored"] == 0
    assert report["temporal-reasoning"]["accuracy"] is None
    assert report["temporal-reasoning"]["provider_failures"] == 1


def test_aggregate_carries_cost_columns_and_an_overall_row() -> None:
    report = _aggregated_report()

    assert report["overall"]["n"] == 2
    assert report["multi-session"]["mean_est_prompt_tokens"] == 1500
    # The user prompt alone is not what the model is charged for. Every token
    # figure this stand produced before 2026-08-31 was short by the system
    # prompt, while the numbers the field publishes are whole-context.
    assert report["multi-session"]["mean_est_total_prompt_tokens"] == 1720
    # `accuracy` is a substring test and cannot match a free-text answer; every
    # figure the backlog compares against is a judge score, so the report has to
    # carry one when the judged rows exist.
    assert report["multi-session"]["judge_accuracy"] == 1.0


def test_gold_aliases_in_parentheses_count_as_the_answer() -> None:
    gold = "University of California, Los Angeles (UCLA)"
    hypothesis = "your Bachelor's degree was completed at UCLA."

    assert longmemeval_score.contains_answer(gold, hypothesis)
    assert not longmemeval_score.contains_answer(gold, "it was at MIT")
