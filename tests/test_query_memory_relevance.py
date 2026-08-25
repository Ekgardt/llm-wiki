"""A citation from the right page but the wrong sentence is not support."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def test_a_span_stating_a_different_figure_is_refused():
    """The shape an operator acts on: right page, wrong number."""
    from query_memory import GroundedQAError, _require_citation_touches_claim

    with pytest.raises(GroundedQAError, match="different figures"):
        _require_citation_touches_claim(
            "the owner lease expires after 30 seconds",
            "the owner lease is refreshed every 10 seconds",
        )


def test_a_span_carrying_the_same_figure_passes():
    from query_memory import _require_citation_touches_claim

    _require_citation_touches_claim(
        "the owner lease expires after 30 seconds",
        "expiry is 30 seconds after the last heartbeat",
    )


def test_a_span_without_figures_may_still_support_a_numeric_claim():
    """A span can spell the number out, so silence there is not a refusal."""
    from query_memory import _require_citation_touches_claim

    _require_citation_touches_claim(
        "the window is 90 days",
        "the retention window is ninety days for every hot artifact",
    )


def test_a_flag_that_differs_is_refused_like_a_number():
    from query_memory import GroundedQAError, _require_citation_touches_claim

    with pytest.raises(GroundedQAError, match="different figures"):
        _require_citation_touches_claim(
            "retire dead tasks with --include-dead",
            "purge exports first with --export before anything is removed",
        )
