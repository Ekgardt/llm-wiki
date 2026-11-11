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

from tests.modules.longmemeval_coverage.fixtures import _EVIDENCE, _QUESTION
from tests.modules.longmemeval_score.prompt_metrics_fixtures import _overall

BENCHMARK_DIR = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_coverage  # noqa: E402
import longmemeval_vault  # noqa: E402


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


def test_the_printed_table_carries_both_figures_with_their_denominators() -> None:
    """A share without its denominator is what let one run be quoted two ways."""
    import run_longmemeval

    columns = run_longmemeval._evidence_columns(_overall())

    assert columns == "1/2 2/3"
