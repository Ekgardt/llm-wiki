"""One run must not offer two unexplained numbers for how much evidence arrived.

On the recorded run of 2026-09-17 three different figures were readable as
"coverage": what search returned (`all_sessions` 0.912), what the compiler could
place (`evidence_requested`/`evidence_missed`, a flat 1.0), and whether the gold
string reached the prompt (0.508). None of them was printed, and the last one is
undefined for a sixth of the questions.

So each is named for the stage it measures, every share is printed beside its
denominator, and the measurement that works for every question type — the turns
the dataset itself flags as carrying the answer — is recorded on the row.

See `docs/research/2026-09-18-a-rubric-is-not-a-miss.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_coverage  # noqa: E402
import longmemeval_score  # noqa: E402
import longmemeval_vault  # noqa: E402

_EVIDENCE = "the user upgraded to the paid tier of the editing suite on a friday"
_OTHER = "the user asked about which microphone to buy for a podcast"

_QUESTION = {
    "answer_session_ids": ["s1"],
    "haystack_sessions": [
        [
            {"role": "user", "content": _EVIDENCE, "has_answer": True},
            {"role": "user", "content": _OTHER},
        ]
    ],
}


def _row(category: str, **fields: object) -> dict:
    return {
        "category": category,
        "question_type": category,
        "gold": "the paid tier",
        "hypothesis": "You moved to the paid tier.",
        "status": "answered",
        **fields,
    }


# A gold that is a span somebody said, and it reached the prompt.
_SPAN_GOLD = _row(
    "multi-session",
    gold_in_prompt=True,
    evidence_turns_labelled=1,
    evidence_in_prompt=True,
)

# A gold that is a rubric: no prompt can contain it, so the gold signal is held
# out — but the dataset's evidence turn still reached the reader and is counted.
_RUBRIC_GOLD = _row(
    "single-session-preference",
    gold_in_prompt=False,
    evidence_turns_labelled=1,
    evidence_in_prompt=True,
)

# A span gold whose evidence never arrived: a real miss, and it must stay one.
_MISSING_EVIDENCE = _row(
    "temporal-reasoning",
    gold_in_prompt=False,
    evidence_turns_labelled=1,
    evidence_in_prompt=False,
)

_ALL = [_SPAN_GOLD, _RUBRIC_GOLD, _MISSING_EVIDENCE]


def _overall() -> dict:
    return longmemeval_score.aggregate(_ALL)["overall"]


def test_the_rubric_row_leaves_the_gold_text_denominator() -> None:
    """Two of three questions have a gold a prompt could carry, and one did."""
    overall = _overall()

    assert (overall["gold_text_in_prompt"], overall["gold_text_applicable"]) == (1, 2)
    assert (overall["gold_text_not_applicable"], overall["gold_text_in_prompt_share"]) == (1, 0.5)


def test_the_evidence_figure_counts_every_type_including_the_rubric_one() -> None:
    """The dataset labels the answer-carrying turns, so this is always defined."""
    overall = _overall()

    assert (overall["evidence_in_prompt"], overall["evidence_measured"]) == (2, 3)
    assert overall["evidence_in_prompt_share"] == 0.6667


def test_a_question_whose_evidence_never_arrived_is_still_a_miss() -> None:
    """Holding out the undefined case must not soften the defined one."""
    missed = longmemeval_score.aggregate([_MISSING_EVIDENCE])["temporal-reasoning"]

    assert (missed["evidence_in_prompt"], missed["evidence_measured"]) == (0, 1)
    assert missed["evidence_in_prompt_share"] == 0.0


def test_a_run_that_never_recorded_the_evidence_reports_none_not_zero() -> None:
    """Runs before 2026-09-18 have no such field, and absence is not a failure."""
    old = longmemeval_score.aggregate([_row("multi-session", gold_in_prompt=True)])["multi-session"]

    assert (old["evidence_measured"], old["evidence_in_prompt"]) == (0, 0)
    assert old["evidence_in_prompt_share"] is None


def test_a_question_that_never_reached_a_prompt_is_in_no_denominator() -> None:
    """A harness failure built no prompt; counting it would invent a miss."""
    failed = _row("multi-session", status="error", error_kind="harness_failure")
    report = longmemeval_score.aggregate([_SPAN_GOLD, failed])["multi-session"]

    assert (report["n"], report["scored"], report["gold_text_applicable"]) == (2, 1, 1)
    assert report["gold_text_in_prompt_share"] == 1.0


def test_the_evidence_turns_of_a_prompt_are_read_from_the_dataset_labels() -> None:
    """The flagged turn is found in the prompt; the unflagged one is not asked about."""
    seen = longmemeval_coverage.evidence_in_text(_QUESTION, f"context: {_EVIDENCE.upper()}")

    assert (seen["turns_labelled"], seen["turns_in_text"]) == (1, 1)
    assert seen["all_turns"] is True


def test_a_prompt_without_the_evidence_turn_says_so() -> None:
    absent = longmemeval_coverage.evidence_in_text(_QUESTION, f"context: {_OTHER}")

    assert (absent["turns_labelled"], absent["turns_in_text"]) == (1, 0)
    assert absent["all_turns"] is False


def test_the_worker_records_both_signals_from_the_prompt_it_sent() -> None:
    """The rubric case: no gold text in the prompt, and the evidence there anyway."""
    metrics: dict = {}
    windows = longmemeval_coverage.evidence_turns(_QUESTION)
    longmemeval_vault._record_prompt_evidence(metrics, f"ctx: {_EVIDENCE}", "a rubric gold", windows)

    assert (metrics["gold_in_prompt"], metrics["evidence_in_prompt"]) == (False, True)
    assert metrics["evidence_turns_labelled"] == 1


def test_a_second_provider_call_never_unsees_what_the_first_one_carried() -> None:
    """The answer call holds the evidence; a later short call must not clear it."""
    metrics: dict = {}
    windows = longmemeval_coverage.evidence_turns(_QUESTION)
    longmemeval_vault._record_prompt_evidence(metrics, f"ctx: {_EVIDENCE}", "x", windows)
    longmemeval_vault._record_prompt_evidence(metrics, "a follow-up question", "x", windows)

    assert metrics["evidence_in_prompt"] is True
    assert metrics["evidence_turns_in_prompt"] == 1


def test_the_compiler_stage_has_its_own_name_and_its_own_counts() -> None:
    """`evidence_requested`/`evidence_missed` is a different stage from retrieval."""
    rows = [{"evidence_requested": 8, "evidence_missed": 2}, {"evidence_requested": 2}]

    assert longmemeval_coverage.compile_evidence(rows) == {
        "chunks_requested": 10,
        "chunks_missed": 2,
        "chunks_placed_share": 0.8,
    }


def test_the_printed_table_carries_both_figures_with_their_denominators() -> None:
    """A share without its denominator is what let one run be quoted two ways."""
    import run_longmemeval

    columns = run_longmemeval._evidence_columns(_overall())

    assert columns == "1/2 2/3"
