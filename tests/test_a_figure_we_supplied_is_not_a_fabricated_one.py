"""A number we handed the model for a span is not a number it made up.

A daily log is named `knowledge/daily/2023-11-03.md`, so the evidence manifest
tells the model the date of the session, and the model writes "in a session
captured on 2023-11-03". The date is nowhere in the quoted bytes — it is in the
file name — so the figure gate saw a claim stating figures the span does not
state, and dropped the claim that carried the answer.

Measured across three runs of 200 questions on 2026-09-07: of 399 answered
questions, 176 had at least one claim dropped by a gate and the answer
published anyway. Those are wrong 23.3% of the time against 3.1% where nothing
was dropped.

See `docs/research/2026-09-07-a-dropped-claim-and-a-wrong-answer.md`.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import query_memory  # noqa: E402
from query_memory import GroundedQAError  # noqa: E402

SPAN = "I see Dr. Smith every week, and she has helped me for 2 years."
CLAIM = 'You see Dr. Smith every week — in a session captured on 2023-11-03 you said so.'


def test_a_date_the_manifest_states_counts_as_the_span_naming_it():
    query_memory._require_figures_agree(
        CLAIM, SPAN, "knowledge/daily/2023-11-03.md"
    )


def test_without_the_manifest_the_same_pair_is_refused():
    """The old behaviour, kept as the thing being fixed."""
    with pytest.raises(GroundedQAError, match="different figures"):
        query_memory._require_figures_agree(CLAIM, SPAN)


def test_a_figure_in_neither_the_span_nor_the_manifest_is_still_refused():
    with pytest.raises(GroundedQAError, match="different figures"):
        query_memory._require_figures_agree(
            "You saw Dr. Smith 14 times.",
            "I saw her 3 times.",
            "knowledge/daily/2023-11-03.md",
        )


def test_only_the_path_is_taken_from_the_manifest():
    """Offsets and hashes would match almost any figure and end the gate."""
    entry = {
        "relative_path": "knowledge/daily/2023-11-03.md",
        "byte_start": 14,
        "byte_end": 900,
        "line_start": 3,
        "line_end": 7,
        "span_sha256": "9" * 64,
        "text": SPAN,
    }

    assert query_memory.supplied_figures(entry) == "knowledge/daily/2023-11-03.md"

    with pytest.raises(GroundedQAError, match="different figures"):
        query_memory._require_figures_agree(
            "You saw her 14 times.", "I saw her 3 times.", entry["relative_path"]
        )


def test_a_span_with_no_figures_still_supports_a_numeric_claim():
    query_memory._require_figures_agree(
        "You saw her 3 times.", "I saw her a few times.", "knowledge/notes/a.md"
    )


def test_the_word_overlap_gate_does_not_read_the_manifest():
    """Repeating a file name is citing a page, not a sentence."""
    with pytest.raises(GroundedQAError, match="shares no content"):
        query_memory._require_citation_touches_claim(
            "Cucumbers were harvested in autumn.",
            "The kettle needs descaling before anybody visits.",
            supplied_text="knowledge/daily/cucumbers-autumn.md",
        )
