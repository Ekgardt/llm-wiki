"""Our number can stand beside the published ones only on their protocol.

Every LongMemEval figure the field publishes comes from the authors'
`evaluate_qa.py`: a template per question type, abstention questions graded
on whether the model said it could not answer, `'yes' in reply.lower()` as
the label, and both a plain mean and a mean of per-type means in circulation.
This stand now grades on those templates, character for character, names the
judge it used, and writes the authors' hypothesis file for anyone who holds
the key to grade with GPT-4o.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BENCHMARK = Path(__file__).resolve().parents[1] / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import longmemeval_hypotheses  # noqa: E402
import longmemeval_judge  # noqa: E402
import longmemeval_official as official  # noqa: E402


def _row(**overrides) -> dict:
    row = {
        "question_id": "q1",
        "question_type": "single-session-user",
        "category": "single-session-user",
        "question": "What colour is my car?",
        "gold": "blue",
        "hypothesis": "Your car is blue.",
        "status": "answered",
        "reason": None,
    }
    row.update(overrides)
    return row


def test_the_fact_template_is_the_authors_word_for_word():
    prompt = official.official_prompt(_row())

    assert prompt.startswith("I will give you a question, a correct answer, and a response from a model.")
    assert "Question: What colour is my car?\n\nCorrect Answer: blue\n\nModel Response: Your car is blue." in prompt
    assert prompt.endswith("Is the model response correct? Answer yes or no only.")


def test_every_question_type_has_its_own_template():
    kinds = {
        "temporal-reasoning": "do not penalize off-by-one errors",
        "knowledge-update": "some previous information along with an updated answer",
        "single-session-preference": "Rubric: blue",
        "multi-session": "contains all the intermediate steps",
    }
    for kind, marker in kinds.items():
        assert marker in official.official_prompt(_row(question_type=kind))


def test_an_abstention_question_is_graded_on_saying_so():
    row = _row(question_id="q9_abs", status="insufficient_evidence", reason="No session mentions a car.")

    prompt = official.official_prompt(row)

    assert prompt.startswith("I will give you an unanswerable question, an explanation, and a response from a model.")
    assert "Model Response: I don't have that information in our conversation history. No session mentions a car." in prompt
    assert prompt.endswith("Does the model correctly identify the question as unanswerable? Answer yes or no only.")


def test_a_silence_reads_as_a_sentence_and_an_answer_as_itself():
    assert official.official_hypothesis(_row()) == "Your car is blue."
    assert official.official_hypothesis(_row(status="conflicting_evidence", reason="")) == official.SILENCE


def test_the_label_is_the_authors_parse():
    assert official.official_label("Yes.") is True
    assert official.official_label("  YES, it is") is True
    assert official.official_label("No") is False
    assert official.official_label(None) is False


def test_an_unknown_question_type_is_refused_by_name():
    import pytest

    with pytest.raises(ValueError, match="no official template"):
        official.official_prompt(_row(question_type="riddle", category="riddle"))


def test_both_overall_figures_are_reported():
    rows = [
        _row(question_id="a", question_type="single-session-user", official_label=True),
        _row(question_id="b", question_type="single-session-user", official_label=True),
        _row(question_id="c", question_type="temporal-reasoning", official_label=False),
        _row(question_id="d_abs", official_label=True),
        _row(question_id="e", official_label=None),
    ]

    report = official.official_accuracy(rows)

    assert report["n"] == 4
    assert report["accuracy"] == 0.75
    assert report["per_type"] == {"abstention": 1.0, "single-session-user": 1.0, "temporal-reasoning": 0.0}
    assert report["task_averaged"] == round(2 / 3, 4)


def test_the_judge_writes_the_official_label_and_leaves_ours_alone():
    calls: list[tuple[str, str, int]] = []

    def call(prompt: str, system_prompt: str, max_tokens: int) -> str:
        calls.append((prompt, system_prompt, max_tokens))
        return "yes"

    judged = longmemeval_judge._officially_judged_row(_row(), call)

    assert judged["official_label"] is True
    assert "judge_correct" not in judged
    assert calls[0][1] == "" and calls[0][2] == official.MAX_JUDGE_TOKENS


def test_a_row_the_provider_never_reached_carries_no_label():
    judged = longmemeval_judge._officially_judged_row(
        _row(status="error", error="boom", error_kind="provider_deadline"),
        lambda *args: "yes",
    )

    assert judged["official_label"] is None


def test_the_hypothesis_file_is_one_id_and_one_text_per_line(tmp_path):
    results = tmp_path / "r.jsonl"
    results.write_text(
        json.dumps(_row()) + "\n" + json.dumps(_row(question_id="q2_abs", status="insufficient_evidence", reason="")) + "\n"
    )

    assert longmemeval_hypotheses.main(["--results", str(results)]) == 0
    lines = (tmp_path / "r.hypotheses.jsonl").read_text().splitlines()
    assert [json.loads(line) for line in lines] == [
        {"question_id": "q1", "hypothesis": "Your car is blue."},
        {"question_id": "q2_abs", "hypothesis": official.SILENCE},
    ]


def test_the_report_names_the_protocol_and_the_judge(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "claude")

    report = longmemeval_judge._report_for("official", [_row(official_label=True)])

    assert report["protocol"].startswith("longmemeval/evaluate_qa.py")
    assert report["judge"] == "claude"
    assert report["accuracy"] == 1.0


def test_the_report_says_what_the_tokens_bought():
    rows = [
        _row(question_id="a", official_label=True, est_total_prompt_tokens=8000, total_seconds=40),
        _row(question_id="b", official_label=False, est_total_prompt_tokens=12000, total_seconds=60),
    ]

    figures = longmemeval_judge.efficiency(rows, "official")

    assert figures["prompt_tokens_mean"] == 10000.0
    assert figures["correct_per_1k_tokens"] == 0.05
    assert figures["seconds_per_correct"] == 100.0
    assert longmemeval_judge.efficiency([], "official")["correct_per_1k_tokens"] is None
