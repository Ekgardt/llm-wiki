"""The calendar does the arithmetic a date question asks for.

Run 1 of the LongMemEval stand, 2026-09-08: date questions were right 16
times in 24. Two answers named the event and its date and never subtracted
("How many days ago did I attend a networking event?" — the answer says
2022-03-09, the gold says 26). A third was refused by our own gate because a
gap between an event and the day the question is asked has one span, not
two. The model reads well and subtracts badly, which is the program-aided
reasoning finding (arXiv:2211.10435): let code do the arithmetic and the
model the reading.

So a claim that declares `derivation: difference` puts its dates in
`inputs`, the calendar computes the gap — against the question's own date
when only one event date is given — and when the claim's text does not state
that figure within a day, the answer is generated once more with the
computed gaps beside the question as data. The same move entity clustering
makes for counts, and at most one regeneration per question.
See `docs/research/2026-09-08-the-calendar-does-the-arithmetic.md`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date

DIFFERENCE = "difference"
_ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_NUMBER = re.compile(r"\b(\d+)\b")
# The official judge forgives off-by-one on day counts, and so does this check.
DAY_TOLERANCE = 1


def difference_claims(answer: Mapping[str, object]) -> list[Mapping[str, object]]:
    claims = answer.get("claims") or ()
    return [claim for claim in claims if claim.get("derivation") == DIFFERENCE]


def _parsed(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _iso_texts(values: Sequence[object]) -> list[str]:
    texts = " ".join(str(value) for value in values)
    return list(dict.fromkeys(_ISO.findall(texts)))


def _dates_in(values: Sequence[object]) -> list[date]:
    """Every distinct ISO date in the values, in order of first appearance."""
    parsed = (_parsed(text) for text in _iso_texts(values))
    return [day for day in parsed if day is not None]


def _pair(claim: Mapping[str, object], anchor: date | None) -> tuple[date, date] | None:
    """The two dates the gap is between: both given, or one given and the day asked."""
    dates = _dates_in(claim.get("inputs") or ())
    if len(dates) >= 2:
        return dates[0], dates[1]
    if len(dates) == 1 and anchor is not None:
        return dates[0], anchor
    return None


def computed_gaps(
    answer: Mapping[str, object], anchor: date | None
) -> list[tuple[Mapping[str, object], date, date, int]]:
    """Each difference claim with its two dates and the gap in days."""
    gaps = []
    for claim in difference_claims(answer):
        pair = _pair(claim, anchor)
        if pair is not None:
            gaps.append((claim, pair[0], pair[1], abs((pair[1] - pair[0]).days)))
    return gaps


def _accepted(days: int) -> set[int]:
    """The figures that would count as stating this gap: days within one, or weeks."""
    nearby = set(range(days - DAY_TOLERANCE, days + DAY_TOLERANCE + 1))
    return nearby | {days // 7, round(days / 7)}


def states_the_gap(claim: Mapping[str, object], days: int) -> bool:
    """Whether the claim's text already carries the computed figure."""
    text = _ISO.sub(" ", str(claim.get("text") or ""))
    numbers = {int(number) for number in _NUMBER.findall(text)}
    return bool(numbers & _accepted(days))


def unstated_gaps(answer: Mapping[str, object], anchor: date | None) -> list[tuple]:
    return [gap for gap in computed_gaps(answer, anchor) if not states_the_gap(gap[0], gap[3])]


def _in_weeks(days: int) -> str:
    weeks, rest = divmod(days, 7)
    return f"{weeks} weeks and {rest} days"


def calendar_note(gaps: Sequence[tuple]) -> str:
    """The computed gaps as data placed beside the question, or nothing."""
    if not gaps:
        return ""
    lines = "\n".join(
        f"- from {first} to {second}: {days} days ({_in_weeks(days)})"
        for _claim, first, second, days in gaps
    )
    return (
        "<calendar>\n"
        "Computed by the calendar from the dates the evidence gave; "
        "state the figure the question asks for:\n" + lines + "\n</calendar>\n"
    )
