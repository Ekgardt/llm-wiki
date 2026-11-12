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

from tests.modules.longmemeval_score.refusals_fixtures import (
    _CORRECT_SILENCE,
    _INVENTED_ANSWER,
    _REFUSED_WITH_EVIDENCE,
    _REFUSED_WITHOUT_EVIDENCE,
)

BENCHMARK_DIR = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_judge  # noqa: E402
import longmemeval_score  # noqa: E402
import run_longmemeval  # noqa: E402


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
