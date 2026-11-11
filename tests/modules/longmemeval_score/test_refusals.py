"""Original deterministic metric assertions, unchanged."""
from __future__ import annotations

import sys
from pathlib import Path

from tests.modules.longmemeval_score.refusals_fixtures import (
    _CORRECT_SILENCE,
    _INVENTED_ANSWER,
    _REFUSED_WITH_EVIDENCE,
    _REFUSED_WITHOUT_EVIDENCE,
    _row,
)

BENCHMARK_DIR = Path(__file__).resolve().parents[3] / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_score  # noqa: E402


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
