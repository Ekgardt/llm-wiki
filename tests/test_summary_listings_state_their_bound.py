"""Two lists in the summary were unbounded and said nothing about it.

`entry_points` and `routes` were fetched at `max_rows=10_000` and returned
whole, with no count and no truncation flag, while every list beside them —
hotspots, communities — states a limit and whether it clipped. A reader could
not tell a complete listing from a clipped one, and the listing grew with the
repository.

Measured on this repository 2026-08-29: 97 entry points, every one a script's
`main`, cost 7 367 of the summary's 24 430 characters. Bounded and counted,
the whole summary falls from 6 107 tokens to 4 910.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import code_graph  # noqa: E402


def _rows(count: int) -> list[dict]:
    return [{"file": f"f{index}.py", "line": index} for index in range(count)]


def test_a_long_listing_is_cut_and_says_so() -> None:
    fields = code_graph._listing_fields(_rows(97), "entry_points")

    assert len(fields["entry_points"]) == code_graph.SUMMARY_LISTING_LIMIT
    assert fields["entry_points_count"] == 97
    assert fields["entry_points_truncated"] is True
    assert fields["entry_points_limit"] == code_graph.SUMMARY_LISTING_LIMIT


def test_a_short_listing_is_whole_and_says_so() -> None:
    fields = code_graph._listing_fields(_rows(3), "routes")

    assert fields["routes"] == _rows(3)
    assert fields["routes_count"] == 3
    assert fields["routes_truncated"] is False


def test_an_empty_listing_still_carries_its_count() -> None:
    """Zero routes is an answer; an absent count is not."""
    fields = code_graph._listing_fields([], "routes")

    assert fields["routes"] == []
    assert fields["routes_count"] == 0
    assert fields["routes_truncated"] is False


def test_the_cut_keeps_the_head_of_the_listing() -> None:
    """Order carries meaning here, so the bound takes from the tail."""
    fields = code_graph._listing_fields(_rows(50), "entry_points")

    assert fields["entry_points"] == _rows(50)[: code_graph.SUMMARY_LISTING_LIMIT]


def test_the_bound_is_the_summary_s_own_number() -> None:
    """One rule for every list in the summary, not a fourth invented number."""
    assert code_graph.SUMMARY_LISTING_LIMIT == code_graph.COMMUNITY_LIMIT
