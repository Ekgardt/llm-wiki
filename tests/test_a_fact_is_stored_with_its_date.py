""""Last Thursday" is a date only if something joins it to a day.

A session says "I met her last Thursday". The daily entry keeps those words and
the day it was captured, and until 2026-09-03 nothing ever joined the two. Asked
later which day that was, the answerer held both halves and refused anyway,
because our contract requires a claim to be carried by a cited span and no span
stated the resolved date.

Measured on the LongMemEval stand that day: of nine substantive refusals in
fifty questions, four were this. The recorded reasons name the phrase and the
capture timestamp in the same sentence, then decline.

The join now happens where the anchor is certain — at write time — and the
resolved date becomes ordinary citable text. This is the what-where-when shape:
the episode is stored with its time bound to it rather than reconstructed later
from a context that may be gone.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import temporal_anchor  # noqa: E402

# A Wednesday.
ANCHOR = date(2023, 5, 31)


def test_a_named_weekday_resolves_backwards_to_the_most_recent_one() -> None:
    found = temporal_anchor.resolutions("I met her last Thursday on the subway", ANCHOR)

    assert found["last thursday"] == "2023-05-25"


def test_last_weekday_never_resolves_to_the_anchor_itself() -> None:
    """"Last Wednesday" said on a Wednesday means the week before, not today."""
    found = temporal_anchor.resolutions("last Wednesday", ANCHOR)

    assert found["last wednesday"] == "2023-05-24"


def test_next_weekday_resolves_forwards() -> None:
    found = temporal_anchor.resolutions("the class is next Monday", ANCHOR)

    assert found["next monday"] == "2023-06-05"


def test_yesterday_and_tomorrow_and_the_day_before() -> None:
    found = temporal_anchor.resolutions(
        "yesterday I rested, tomorrow I fly, the day before yesterday I packed", ANCHOR
    )

    assert found["yesterday"] == "2023-05-30"
    assert found["tomorrow"] == "2023-06-01"
    assert found["the day before yesterday"] == "2023-05-29"


def test_a_count_of_days_or_weeks_ago() -> None:
    found = temporal_anchor.resolutions("I finished it three days ago, and two weeks ago I started", ANCHOR)

    assert found["three days ago"] == "2023-05-28"
    assert found["two weeks ago"] == "2023-05-17"


def test_a_digit_count_works_as_well_as_a_word() -> None:
    found = temporal_anchor.resolutions("10 days ago", ANCHOR)

    assert found["10 days ago"] == "2023-05-21"


def test_a_month_is_left_alone_because_it_has_no_single_answer() -> None:
    """A confident wrong date is worse than no date."""
    found = temporal_anchor.resolutions("two months ago I moved", ANCHOR)

    assert found == {}


def test_text_with_no_relative_date_gets_no_footer() -> None:
    assert temporal_anchor.annotation("I like tea.", ANCHOR) == ""


def test_the_footer_reads_as_a_sentence_a_citation_can_quote() -> None:
    footer = temporal_anchor.annotation("I met her last Thursday", ANCHOR)

    assert "resolved against this entry's day" in footer
    assert "last thursday = 2023-05-25" in footer


def test_one_phrase_repeated_resolves_once() -> None:
    found = temporal_anchor.resolutions("yesterday, and again yesterday", ANCHOR)

    assert list(found) == ["yesterday"]


def test_the_footer_is_bounded() -> None:
    """An entry is bounded, so what is appended to it has to be too."""
    text = " ".join(f"{n} days ago" for n in range(1, 30))

    assert len(temporal_anchor.resolutions(text, ANCHOR)) == temporal_anchor.MAX_RESOLUTIONS
