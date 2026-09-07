"""One LongMemEval category is not graded on facts, and grading it so scores nil.

Measured on this vault 2026-09-01, three runs of 200: `single-session-preference`
read 0.0000 with a spread of 0.0 — flat, three times, twelve questions, while
the next weakest category moved by 0.0172 between runs. A flat zero is a
mechanism, not a quality gap.

The gold text there is not a value to match. It describes what a good answer
would take into account — "the user would prefer responses that utilize their
existing resources, such as their Suica card and TripIt app" — and the benchmark
grades it against a rubric for that reason, reporting token-overlap metrics as
inapplicable. Our judge asked "does the model answer state the same fact", which
no correct answer to such a question can satisfy.

See `docs/research/2026-09-01-a-category-graded-by-the-wrong-question.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_judge  # noqa: E402

_PREFERENCE = {
    "category": "single-session-preference",
    "question": "I'm a bit anxious about getting around Tokyo. Any tips?",
    "gold": "The user would prefer responses that utilize their existing "
    "resources, such as their Suica card and TripIt app.",
    "hypothesis": "You already have a Suica card, so top it up on arrival.",
}

_FACT = {
    "category": "temporal-reasoning",
    "question": "How old was I when I moved?",
    "gold": "21",
    "hypothesis": "You were 21.",
}


def test_a_preference_row_is_graded_against_the_rubric() -> None:
    assert (
        longmemeval_judge.system_prompt_for(_PREFERENCE)
        is longmemeval_judge.RUBRIC_SYSTEM_PROMPT
    )


def test_every_other_category_keeps_the_fact_prompt() -> None:
    """The change is one category wide; nothing else may loosen."""
    assert (
        longmemeval_judge.system_prompt_for(_FACT)
        is longmemeval_judge.JUDGE_SYSTEM_PROMPT
    )


def test_the_rubric_prompt_does_not_ask_for_a_fact() -> None:
    prompt = longmemeval_judge.judge_prompt(_PREFERENCE)

    assert "same fact" not in prompt
    assert "respect that preference" in prompt
    assert "What the user would prefer:" in prompt


def test_the_fact_prompt_is_unchanged() -> None:
    prompt = longmemeval_judge.judge_prompt(_FACT)

    assert "state the same fact as the gold answer" in prompt


def test_the_rubric_names_a_refusal_as_wrong() -> None:
    """The current answers are empty; the grader must not reward that."""
    assert "refusal" in longmemeval_judge.RUBRIC_SYSTEM_PROMPT
    assert "An empty" in longmemeval_judge.RUBRIC_SYSTEM_PROMPT


def test_the_rubric_does_not_require_every_item_to_be_named() -> None:
    """A rubric asks what was taken into account, not what was recited."""
    assert "even without naming every one of them" in (
        longmemeval_judge.RUBRIC_SYSTEM_PROMPT
    )


def test_a_missing_category_is_graded_on_facts() -> None:
    """Unknown shape, strictest rule: never grade something as a rubric by default."""
    assert (
        longmemeval_judge.system_prompt_for({"gold": "x", "hypothesis": "x"})
        is longmemeval_judge.JUDGE_SYSTEM_PROMPT
    )


def test_an_unanswered_preference_row_still_needs_no_judge() -> None:
    """An abstention is settled before the judge; the rubric changes nothing there."""
    assert not longmemeval_judge.needs_judging(
        {**_PREFERENCE, "status": "insufficient_evidence", "hypothesis": ""}
    )
