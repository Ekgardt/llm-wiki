""""Last week" is a stretch of days, and no day inside it is written as a fact.

Third audit, 2026-09-17 (retrieval L14). The phrase resolved to anchor − 7 and that day was
written into the daily entry as a dated line the answerer could cite — a date the user never
named, against the module's own "only what is stated". ISO 8601 identifies the week itself,
Monday to Sunday, so the value exists and is a pair of days.
See `docs/research/2026-09-17-last-week-is-a-week.md`.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import temporal_anchor  # noqa: E402

# A Wednesday: its own week is Mon 2023-05-29 .. Sun 2023-06-04.
ANCHOR = date(2023, 5, 31)
SAID = "**user:** I bought the amplifier last week and it sounds great."


def test_last_week_is_the_previous_monday_to_sunday() -> None:
    found = temporal_anchor.spans(SAID, ANCHOR)

    assert found["last week"] == ("2023-05-22", "2023-05-28")


def test_a_week_is_the_whole_week_from_a_monday_too() -> None:
    """On a Monday "last week" is still the week before, not the seven days behind."""
    found = temporal_anchor.spans("**user:** last week", date(2023, 5, 29))

    assert found["last week"] == ("2023-05-22", "2023-05-28")


def test_no_day_of_that_week_is_written_into_the_entry_as_a_fact() -> None:
    resolved = temporal_anchor.resolutions(SAID, ANCHOR)
    written = temporal_anchor.annotation(SAID, ANCHOR)

    assert "last week" not in resolved
    assert written == ""


def test_the_search_window_is_that_whole_week() -> None:
    assert temporal_anchor.window("What did I buy last week?", ANCHOR) == (
        "2023-05-22",
        "2023-05-28",
    )


def test_the_query_carries_every_day_of_that_week() -> None:
    carried = temporal_anchor.query_with_dates("What did I buy last week?", ANCHOR)

    days = ["2023-05-22", "2023-05-25", "2023-05-28"]
    assert all(day in carried for day in days)
    assert carried.startswith("What did I buy last week?")


def test_a_point_and_a_stretch_in_one_question_span_both() -> None:
    """A day keeps its three-day neighbourhood; the stretch is taken exactly."""
    asked = temporal_anchor.window("I flew yesterday, after buying it last week", ANCHOR)

    assert asked == ("2023-05-22", "2023-06-02")
