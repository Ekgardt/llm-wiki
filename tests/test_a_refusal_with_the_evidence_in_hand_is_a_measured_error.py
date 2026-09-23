"""A refusal scored wrong by construction, and no field said it was a refusal.

`longmemeval_judge.needs_judging` never sends a refusal to the judge, so the
deterministic substring test stands in and reads the empty hypothesis as a miss.
That verdict is right — a refusal on a question that had an answer is a loss —
but it left the report with no way to say how many of its losses were refusals,
or whether those refusals had the evidence in front of them. On the recorded run
of 2026-09-18, 26 of the 75 losses on the 470 answerable questions are refusals,
13 of them with the gold string in the prompt word for word.
`longmemeval_coverage.failure_split` cannot see them either: it counts rows the
judge called wrong, and a refusal has no judge verdict at all.

The same decision made on a question whose right answer is silence is a win no
published competitor measures. Both directions are reported, each figure over
the denominator it was taken on.

See `docs/research/2026-09-19-a-number-names-its-stand.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_judge  # noqa: E402
import longmemeval_score  # noqa: E402
import run_longmemeval  # noqa: E402


def _row(**fields: object) -> dict:
    return {
        "question_id": "q",
        "category": "multi-session",
        "question_type": "multi-session",
        "gold": "the paid tier",
        "hypothesis": "",
        "status": "insufficient_evidence",
        **fields,
    }


# The loss we could win: the reader declined a question whose every labelled
# evidence turn, and the gold string itself, were in the prompt it received.
_REFUSED_WITH_EVIDENCE = _row(
    gold_in_prompt=True,
    evidence_turns_labelled=2,
    evidence_in_prompt=True,
)

# Retrieval did the refusing: a prompt was built and the evidence was not in it.
_REFUSED_WITHOUT_EVIDENCE = _row(
    question_id="q2",
    gold_in_prompt=False,
    evidence_turns_labelled=2,
    evidence_in_prompt=False,
)

# The win nobody else measures: the gold is an explanation of why nothing can be
# answered, and the system was silent.
_CORRECT_SILENCE = _row(
    question_id="q3",
    category="abstention",
    question_type="abstention",
    is_abstention=True,
    gold="No session says which laptop was bought.",
)

# The same decision failing the other way: it answered where silence was right.
_INVENTED_ANSWER = _row(
    question_id="q4",
    category="abstention",
    question_type="abstention",
    is_abstention=True,
    gold="No session says which laptop was bought.",
    status="answered",
    hypothesis="You bought the fourteen-inch one.",
)


def test_a_refusal_with_the_evidence_in_hand_is_counted_and_named() -> None:
    """Both prompt signals, each over the rows it can be asked about."""
    report = longmemeval_score.abstention_calibration([_REFUSED_WITH_EVIDENCE])

    assert (report["refused"], report["answerable"]) == (1, 1)
    assert (report["refused_with_evidence_in_prompt"], report["refused_evidence_measured"]) == (1, 1)
    assert (
        report["refused_with_gold_text_in_prompt"],
        report["refused_gold_text_applicable"],
    ) == (1, 1)


def test_a_refusal_without_the_evidence_is_counted_apart() -> None:
    """Retrieval refusing and the reader refusing need opposite work."""
    report = longmemeval_score.abstention_calibration(
        [_REFUSED_WITH_EVIDENCE, _REFUSED_WITHOUT_EVIDENCE]
    )

    assert (report["refused"], report["answerable"]) == (2, 2)
    assert (report["refused_with_evidence_in_prompt"], report["refused_evidence_measured"]) == (1, 2)
    assert (
        report["refused_with_gold_text_in_prompt"],
        report["refused_gold_text_applicable"],
    ) == (1, 2)


def test_a_correct_silence_is_a_win_and_sits_in_its_own_denominator() -> None:
    """The abstention questions are not answerable ones; the two never mix."""
    report = longmemeval_score.abstention_calibration(
        [_REFUSED_WITH_EVIDENCE, _CORRECT_SILENCE]
    )

    assert (report["silence_expected"], report["answerable"]) == (1, 1)
    assert report["refused_when_silence_expected"] == 1
    assert report["answered_when_silence_expected"] == 0


def test_answering_where_silence_was_right_is_the_other_error() -> None:
    """A system that answered everything would turn 26 correct silences into 26 inventions."""
    report = longmemeval_score.abstention_calibration([_CORRECT_SILENCE, _INVENTED_ANSWER])

    assert report["silence_expected"] == 2
    assert (report["refused_when_silence_expected"], report["answered_when_silence_expected"]) == (
        1,
        1,
    )
    assert (report["answerable"], report["refused"]) == (0, 0)


def test_a_refusal_that_never_built_a_prompt_is_in_no_evidence_denominator() -> None:
    """Counting it would credit the reader with evidence it never received."""
    never_prompted = _row(question_id="q5")
    report = longmemeval_score.abstention_calibration([never_prompted])

    assert (report["refused"], report["answerable"]) == (1, 1)
    assert report["refused_evidence_measured"] == 0
    assert report["refused_gold_text_applicable"] == 0


def test_a_provider_failure_declined_nothing() -> None:
    """Nothing was produced, so no decision was made to measure."""
    failed = _row(question_id="q6", status="error", error="boom", error_kind="provider_no_response")
    report = longmemeval_score.abstention_calibration([failed])

    assert (report["answerable"], report["refused"]) == (0, 0)
    assert report["silence_expected"] == 0


def test_the_judge_report_carries_the_calibration_beside_the_accuracy() -> None:
    """The judge never sees a refusal, so the judged report has to say so itself."""
    report = longmemeval_judge._report_for("ours", [_REFUSED_WITH_EVIDENCE, _CORRECT_SILENCE])

    assert report["abstention_calibration"]["refused_with_evidence_in_prompt"] == 1
    assert report["abstention_calibration"]["refused_when_silence_expected"] == 1
    assert report["overall"]["judge_accuracy"] == 0.5


def test_the_printed_line_names_every_figure_and_its_denominator() -> None:
    """No bare share: a share without its denominator is quotable two ways."""
    rows = [_REFUSED_WITH_EVIDENCE, _REFUSED_WITHOUT_EVIDENCE, _CORRECT_SILENCE, _INVENTED_ANSWER]
    line = run_longmemeval.abstention_line(longmemeval_score.abstention_calibration(rows))

    assert line == (
        "abstention calibration: refused_of_answerable=2/2 "
        "refused_with_evidence_in_prompt=1/2 refused_with_gold_text_in_prompt=1/2 "
        "refused_when_silence_expected=1/2 answered_when_silence_expected=1/2 "
        "refused_when_twin=0/0 answered_when_twin=0/0 pairs_right=0/0"
    )
