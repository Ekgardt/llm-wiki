#!/usr/bin/env python3
"""Resolve "last Thursday" into a date, at the moment the memory is written.

A session says "I met her last Thursday". The daily entry keeps those words and
the day it was captured, and nothing ever joins the two. Asked later which day
that was, the answerer holds both halves and still refuses, because our contract
requires a claim to be carried by a cited span and no span states the resolved
date. The arithmetic is trivial and no one is allowed to do it.

Measured on the LongMemEval stand 2026-09-03: of nine substantive refusals in
fifty questions, **four were this** — temporal reasoning is the weakest category
we have, and the recorded reasons show the model naming both the phrase and the
capture timestamp before declining.

So the join happens at write time, which is where the anchor is certain. The
resolved date becomes ordinary text inside the entry, citable like any other
sentence, and every existing gate keeps working unchanged. This is the
what-where-when shape: an episode is stored with its time already bound to it,
rather than reconstructed on demand from a context that may be gone.

Deliberately narrow, in three ways.

**Only day granularity.** Today, yesterday, the day before yesterday, tomorrow,
a named weekday with last or next, and a count of days or weeks ago. Months and
years are left alone — "two months ago" has no single correct answer — and so is
everything below a day. "Last night", "this morning", "a few hours ago" are
exactly where a model's sense of elapsed time fails, and they are not resolved.

**Only the user's turns.** A model will write "last night" for an hour ago. The
arithmetic here is ours and the anchor is certain, so nothing depends on a
model's estimate — but a phrase the assistant wrote can be wrong at the source,
and resolving it would turn a loose remark into a dated fact. The user's account
of their own week is the authority the vault already ranks highest.

**Only what is stated.** No date is inferred, defaulted, or guessed; a phrase
that does not match a rule is left as it was written.

See `knowledge/notes/a-fact-is-stored-with-its-date-decision.md`.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

_COUNTS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

_PLAIN = {"today": 0, "yesterday": -1, "tomorrow": 1}

_PLAIN_RE = re.compile(r"\b(today|yesterday|tomorrow)\b", re.IGNORECASE)
_DAY_BEFORE_RE = re.compile(r"\bthe day before yesterday\b", re.IGNORECASE)
_WEEKDAY_RE = re.compile(
    r"\b(last|next|this past)\s+(" + "|".join(WEEKDAYS) + r")\b", re.IGNORECASE
)
_AGO_RE = re.compile(
    r"\b(\d{1,2}|" + "|".join(_COUNTS) + r")\s+(day|days|week|weeks)\s+ago\b",
    re.IGNORECASE,
)
_LAST_WEEK_RE = re.compile(r"\blast week\b", re.IGNORECASE)

# A cap, because an entry is bounded and a footer that grows with the text is a
# second body. Ten distinct dates is far more than any real entry carries.
MAX_RESOLUTIONS = 10


def _count_of(word: str) -> int:
    lowered = word.casefold()
    if lowered.isdigit():
        return int(lowered)
    return _COUNTS.get(lowered, 0)


def _back_to_weekday(anchor: date, weekday: int) -> date:
    """The most recent day with that weekday, strictly before the anchor."""
    delta = (anchor.weekday() - weekday) % 7
    return anchor - timedelta(days=delta or 7)


def _forward_to_weekday(anchor: date, weekday: int) -> date:
    delta = (weekday - anchor.weekday()) % 7
    return anchor + timedelta(days=delta or 7)


def _weekday_date(anchor: date, direction: str, name: str) -> date:
    weekday = WEEKDAYS.index(name.casefold())
    if direction.casefold() == "next":
        return _forward_to_weekday(anchor, weekday)
    return _back_to_weekday(anchor, weekday)


def _plain_hits(text: str, anchor: date) -> list[tuple[str, date]]:
    return [
        (match.group(0).casefold(), anchor + timedelta(days=_PLAIN[match.group(1).casefold()]))
        for match in _PLAIN_RE.finditer(text)
    ]


def _day_before_hits(text: str, anchor: date) -> list[tuple[str, date]]:
    return [
        (match.group(0).casefold(), anchor - timedelta(days=2))
        for match in _DAY_BEFORE_RE.finditer(text)
    ]


def _weekday_hits(text: str, anchor: date) -> list[tuple[str, date]]:
    return [
        (
            match.group(0).casefold(),
            _weekday_date(anchor, match.group(1), match.group(2)),
        )
        for match in _WEEKDAY_RE.finditer(text)
    ]


def _ago_days(count: int, unit: str) -> int:
    return count * 7 if unit.casefold().startswith("week") else count


def _ago_hits(text: str, anchor: date) -> list[tuple[str, date]]:
    hits = []
    for match in _AGO_RE.finditer(text):
        count = _count_of(match.group(1))
        if not count:
            continue
        days = _ago_days(count, match.group(2))
        hits.append((match.group(0).casefold(), anchor - timedelta(days=days)))
    return hits


def _last_week_hits(text: str, anchor: date) -> list[tuple[str, date]]:
    return [
        (match.group(0).casefold(), anchor - timedelta(days=7))
        for match in _LAST_WEEK_RE.finditer(text)
    ]


_FINDERS = (_day_before_hits, _plain_hits, _weekday_hits, _ago_hits, _last_week_hits)


_TURN_RE = re.compile(r"^\*\*(user|assistant):\*\*")


def _turn_role(line: str, current: str) -> str:
    match = _TURN_RE.match(line)
    if not match:
        return current
    return match.group(1)


def spoken_by_the_user(text: str) -> str:
    """The user's own turns, when the text is a rendered conversation.

    A model's sense of elapsed time is unreliable in a way its arithmetic is
    not: it will write "last night" for an hour ago and "a few hours" for a few
    minutes. Nothing here computes a date from a model's estimate — the
    arithmetic is ours and the anchor is the entry's own day — but a phrase the
    assistant wrote can still be wrong at the source, and resolving it would
    turn a loose remark into a dated fact in the memory.

    So only the user's turns are read. The user's statement about their own week
    is the authority the vault already ranks highest, and a wrong date the user
    themselves gave is their record, not our invention.

    Text with no turn markers is not a conversation and is read whole.
    """
    if not _TURN_RE.search(text) and "**user:**" not in text:
        return text
    kept: list[str] = []
    role = ""
    for line in text.splitlines():
        role = _turn_role(line, role)
        kept.append(line if role == "user" else "")
    return "\n".join(kept)


def resolutions(text: str, anchor: date) -> dict[str, str]:
    """Every unambiguous relative date the user stated, as phrase to ISO date.

    First writing wins, so a phrase repeated in one entry resolves once.
    """
    spoken = spoken_by_the_user(text)
    found: dict[str, str] = {}
    for finder in _FINDERS:
        for phrase, resolved in finder(spoken, anchor):
            found.setdefault(phrase, resolved.isoformat())
    return dict(list(found.items())[:MAX_RESOLUTIONS])


def annotation(text: str, anchor: date) -> str:
    """The footer to append to an entry, or an empty string when there is none.

    The phrasing is deliberately plain so that the lexical leg matches it and a
    citation quoting it reads as a sentence rather than as machine output.
    """
    found = resolutions(text, anchor)
    if not found:
        return ""
    lines = [f"- {phrase} = {resolved}" for phrase, resolved in found.items()]
    return "\n**Dates mentioned above, resolved against this entry's day:**\n" + "\n".join(lines) + "\n"
