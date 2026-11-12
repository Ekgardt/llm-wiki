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

import sys
from pathlib import Path

from tests.modules.longmemeval_protocol.fixtures import _row

BENCHMARK = Path(__file__).resolve().parents[1] / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import longmemeval_judge  # noqa: E402
import longmemeval_official as official  # noqa: E402


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
