"""The window is twelve and the seven lane constants are the 2026-09-16 fit, on purpose.

Both were re-examined on 2026-09-18 against everything the runs had recorded, and both were
left where they are — the window because widening it was measured at +0.0010 turn recall and
+0.0000 all-turns, the constants because the only run carrying lane matrices records them
over the reader's window and so cannot tell one order from another at that depth.

These are pins, not derivations. They fail when somebody pastes a new number, which is the
moment a dated research note is owed. See
`docs/research/2026-09-18-the-lane-refit-cannot-see-what-it-threw-away.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _directory in (ROOT / "scripts", ROOT / "benchmark"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import fit_lane_score  # noqa: E402
import lane_score  # noqa: E402
import longmemeval_vault  # noqa: E402

# The fit of 2026-09-16 over 189 questions, in the order `fit_lane_score` reports them.
SHIPPED = (-0.5071, -0.7639, 0.3052, -1.1359, 2.5144, 0.2789, 3.0138)


def test_the_seven_constants_are_the_fit_of_2026_09_16() -> None:
    """Nothing measured since has beaten them, so nothing has replaced them."""
    weights = tuple(round(value, 4) for value in fit_lane_score.shipped_weights())

    assert weights == SHIPPED


def test_the_fitter_and_the_score_name_the_same_seven_terms() -> None:
    """A renamed constant would leave the fitter reporting a term the score never reads."""
    named = tuple(fit_lane_score.FEATURE_NAMES)
    present = tuple(name for name in named if hasattr(lane_score, name))

    assert (len(named), present) == (7, named)


def test_the_reader_window_stays_at_twelve_candidates() -> None:
    """Measured 2026-09-14 over 201 questions: the all-turns share is 0.3682 at both a
    depth of 12 and a depth of 24, so the 13th to 24th candidates carry no evidence the
    reader did not already have, and every one of them costs retrieval and compile work."""
    assert (longmemeval_vault.QA_CANDIDATES, fit_lane_score.READER_DEPTH) == (12, 12)


def test_a_sweep_can_still_widen_the_window_without_editing_the_constant(monkeypatch) -> None:
    """The decision pins the default, not the experiment."""
    monkeypatch.setenv("LLMWIKI_BENCH_QA_CANDIDATES", "24")
    widened = longmemeval_vault._qa_candidates()

    monkeypatch.delenv("LLMWIKI_BENCH_QA_CANDIDATES")
    default = longmemeval_vault._qa_candidates()

    assert (widened, default) == (24, 12)
