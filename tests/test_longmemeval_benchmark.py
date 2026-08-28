"""Offline tests for the LongMemEval harness (MEM-10).

Everything here runs without the dataset, without the network, and without a
provider: what is tested is the deterministic machinery — sampling, scoring,
transcript shaping — not the benchmark result itself.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import longmemeval_data  # noqa: E402
import longmemeval_score  # noqa: E402
import longmemeval_vault  # noqa: E402


def _question(question_id: str, question_type: str) -> dict:
    return {
        "question_id": question_id,
        "question_type": question_type,
        "question": "q",
        "answer": "a",
        "question_date": "2023/05/30 (Tue) 23:40",
        "haystack_dates": [],
        "haystack_session_ids": [],
        "haystack_sessions": [],
    }


def _population() -> list[dict]:
    rows = [_question(f"m{index}", "multi-session") for index in range(300)]
    rows += [_question(f"t{index}", "temporal-reasoning") for index in range(150)]
    rows += [_question(f"k{index}_abs", "knowledge-update") for index in range(50)]
    return rows


def _sampled_ids(seed: int) -> list[str]:
    picked = longmemeval_data.stratified_sample(_population(), 50, seed=seed)
    return [question["question_id"] for question in picked]


def test_the_sample_is_deterministic_for_a_seed() -> None:
    assert _sampled_ids(13) == _sampled_ids(13)


def test_the_sample_has_the_asked_size_without_duplicates() -> None:
    ids = _sampled_ids(13)

    assert len(ids) == 50
    assert len(set(ids)) == 50


def test_the_sample_is_proportional_across_strata() -> None:
    picked = longmemeval_data.stratified_sample(_population(), 50, seed=13)
    types = [question["question_type"] for question in picked]

    assert types.count("multi-session") == 30
    assert types.count("temporal-reasoning") == 15
    assert types.count("knowledge-update") == 5


def test_abstention_questions_are_their_own_category() -> None:
    plain = _question("q1", "knowledge-update")
    abstention = _question("q2_abs", "knowledge-update")

    assert longmemeval_data.category_of(plain) == "knowledge-update"
    assert longmemeval_data.category_of(abstention) == "abstention"


def test_a_missing_dataset_refuses_instead_of_inventing_one(tmp_path: Path) -> None:
    with pytest.raises(longmemeval_data.DatasetUnavailable) as caught:
        longmemeval_data.load_dataset(tmp_path / "absent.json")

    assert "synthetic" in str(caught.value)


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


def test_haystack_dates_become_day_and_time() -> None:
    assert longmemeval_vault.day_of("2023/05/20 (Sat) 02:21") == "2023-05-20"
    assert longmemeval_vault.time_of("2023/05/20 (Sat) 02:21") == "02:21:00"


def test_the_transcript_renders_through_the_products_own_renderer() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from session_evidence import render_transcript

    turns = [
        {"role": "user", "content": "I adopted a puppy named Biscuit."},
        {"role": "assistant", "content": "Congratulations!", "has_answer": True},
    ]
    rendered = render_transcript(longmemeval_vault.transcript_jsonl(turns))

    assert "**user:** I adopted a puppy named Biscuit." in rendered
    assert "**assistant:** Congratulations!" in rendered


def test_the_daily_block_is_capture_shaped_and_dated() -> None:
    block = longmemeval_vault.daily_block("sess-1", "2023/05/20 (Sat) 02:21", "**user:** hi")

    assert block.startswith("## [02:21:00] session_end | sess-1")
    assert "_captured: 2023-05-20 02:21:00_" in block


def test_the_hypothesis_joins_claim_texts() -> None:
    document = {
        "claims": [{"text": "You paid 85 dollars."}, {"text": "The sneakers were new."}],
    }

    assert longmemeval_vault.hypothesis_of(document) == (
        "You paid 85 dollars. The sneakers were new."
    )


def test_provider_failures_get_their_own_error_kind() -> None:
    provider = longmemeval_vault.error_kind(
        ValueError("grounded QA provider returned no response")
    )

    assert provider == "provider_no_response"
    assert provider in longmemeval_score.PROVIDER_ERROR_KINDS


def test_a_deadline_is_a_provider_failure_and_a_gate_is_not() -> None:
    deadline = longmemeval_vault.error_kind(TimeoutError("grounded QA deadline exceeded"))
    gate = longmemeval_vault.error_kind(ValueError("citation precision and recall gates"))

    assert deadline in longmemeval_score.PROVIDER_ERROR_KINDS
    assert gate == "verification_or_gate"


def test_gold_aliases_in_parentheses_count_as_the_answer() -> None:
    gold = "University of California, Los Angeles (UCLA)"
    hypothesis = "your Bachelor's degree was completed at UCLA."

    assert longmemeval_score.contains_answer(gold, hypothesis)
    assert not longmemeval_score.contains_answer(gold, "it was at MIT")
