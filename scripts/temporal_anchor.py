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

A phrase that names a stretch of days rather than one — "last week" — is kept as
a stretch, by `spans`, and only widens a search. It is never written into an
entry as a dated fact, because the day inside the week is precisely what the
user did not say. See `docs/research/2026-09-17-last-week-is-a-week.md`.

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
from collections.abc import Sequence
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

_PLAIN = {"the day before yesterday": -2, "today": 0, "yesterday": -1, "tomorrow": 1}

# One pattern, the longest phrase first: matches do not overlap, so the
# "yesterday" inside "the day before yesterday" is consumed with its phrase and
# is not dated a second time, a day off.
_PLAIN_RE = re.compile(r"\b(" + "|".join(_PLAIN) + r")\b", re.IGNORECASE)
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


def _last_week_span(anchor: date) -> tuple[date, date]:
    """Monday to Sunday of the ISO week before the anchor's own week.

    ISO 8601 identifies a week, not a day inside it: weeks run Monday (day 1)
    to Sunday (day 7). "Last week" therefore has a correct machine value and it
    is a pair of days — collapsing it to anchor − 7 wrote into the memory, as a
    dated fact, a day the user never named.
    See `docs/research/2026-09-17-last-week-is-a-week.md`.
    """
    this_monday = anchor - timedelta(days=anchor.weekday())
    first = this_monday - timedelta(days=7)
    return first, first + timedelta(days=6)


def _last_week_spans(text: str, anchor: date) -> list[tuple[str, tuple[date, date]]]:
    span = _last_week_span(anchor)
    return [(match.group(0).casefold(), span) for match in _LAST_WEEK_RE.finditer(text)]


_FINDERS = (_plain_hits, _weekday_hits, _ago_hits)
# Expressions that name a stretch of days rather than one. Read by the query side
# only: a span is not the unambiguous single date this module is allowed to write
# into an entry, so no dated line is ever produced for one.
_SPAN_FINDERS = (_last_week_spans,)


# Multiline, because an entry begins with its heading: a turn marker opens a
# line, not the text.
_TURN_RE = re.compile(r"^\*\*(user|assistant):\*\*", re.MULTILINE)


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


def spans(text: str, anchor: date) -> dict[str, tuple[str, str]]:
    """Every stretch of days the user named, as phrase to (first ISO day, last ISO day).

    Separate from `resolutions` because the two are read differently: a point is
    written into the entry as a dated fact, a span only widens a search. First
    writing wins, and the same cap applies.
    """
    spoken = spoken_by_the_user(text)
    found: dict[str, tuple[str, str]] = {}
    for finder in _SPAN_FINDERS:
        for phrase, (first, last) in finder(spoken, anchor):
            found.setdefault(phrase, (first.isoformat(), last.isoformat()))
    return dict(list(found.items())[:MAX_RESOLUTIONS])


def resolutions(text: str, anchor: date) -> dict[str, str]:
    """Every unambiguous relative date the user stated, as phrase to ISO date.

    First writing wins, so a phrase repeated in one entry resolves once. A phrase
    naming a stretch of days is not here — see `spans`.
    """
    spoken = spoken_by_the_user(text)
    found: dict[str, str] = {}
    for finder in _FINDERS:
        for phrase, resolved in finder(spoken, anchor):
            found.setdefault(phrase, resolved.isoformat())
    return dict(list(found.items())[:MAX_RESOLUTIONS])


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

# A sentence is quoted whole where it fits and cut where it does not, because a
# calendar line is a pointer to the entry, not a second copy of it.
MAX_EVENT_CHARS = 220


def _sentences(text: str) -> list[str]:
    parts = [part.strip() for line in text.splitlines() for part in _SENTENCE_SPLIT.split(line)]
    return [part for part in parts if part]


def _clipped(sentence: str) -> str:
    if len(sentence) <= MAX_EVENT_CHARS:
        return sentence
    return sentence[:MAX_EVENT_CHARS].rstrip() + "…"


def _sentence_for(phrase: str, sentences: Sequence[str]) -> str:
    for sentence in sentences:
        if phrase in sentence.casefold():
            return _clipped(sentence.removeprefix("**user:** ").strip())
    return ""


def events(text: str, anchor: date) -> list[tuple[str, str]]:
    """What the user said happened, keyed by the date it happened on.

    The date comes from our own arithmetic and the sentence comes from the
    user's own words, so the line is a pointer into the entry rather than a
    paraphrase of it. Sorted by date, because a calendar is read that way.
    """
    spoken = spoken_by_the_user(text)
    sentences = _sentences(spoken)
    dated = [
        (resolved, _sentence_for(phrase, sentences))
        for phrase, resolved in resolutions(text, anchor).items()
    ]
    return sorted((day, said) for day, said in dated if said)


def annotation(text: str, anchor: date) -> str:
    """The calendar footer for an entry, or an empty string when it has none.

    Every claim this system publishes has to cite bytes inside a Markdown file,
    so the calendar is written into the entry rather than kept only as rows. The
    phrasing is plain on purpose: the lexical leg matches it, and a citation
    quoting it reads as a sentence rather than as machine output.
    """
    dated = events(text, anchor)
    if not dated:
        return ""
    lines = [f"- {day} — {said}" for day, said in dated]
    header = "\n**What happened, by date (resolved against this entry's day):**\n"
    return header + "\n".join(lines) + "\n"


def query_with_dates(query: str, anchor: date) -> str:
    """The query, plus the dates its own relative expressions resolve to.

    "Which book did I finish a week ago" contains no date, so nothing in it can
    match a dated line however well that line is written. The same arithmetic
    that dates an entry dates the question, and the resolved dates join the
    query as ordinary terms — which is what makes the calendar reachable.
    """
    dates = [
        *resolutions(query, anchor).values(),
        *_neighbourhood(query, anchor),
        *_span_days(query, anchor),
    ]
    if not dates:
        return query
    return query + " " + " ".join(dict.fromkeys(dates))


def _span_days(text: str, anchor: date) -> list[str]:
    """Every day of every stretch the text named, so the lexical leg reaches any of them."""
    days: list[str] = []
    for first, last in spans(text, anchor).values():
        start, end = date.fromisoformat(first), date.fromisoformat(last)
        days.extend(
            (start + timedelta(days=offset)).isoformat()
            for offset in range((end - start).days + 1)
        )
    return days


# "Four weeks ago" in a question means about four weeks; the day it resolves to
# is the centre of a neighbourhood, and the days around it join the query so
# the lexical leg reaches an entry three days off. Days stay exact.
NEIGHBOURHOOD_DAYS = 3


def _neighbourhood(text: str, anchor: date) -> list[str]:
    """The days around every "N weeks ago" the text resolves to."""
    around: list[str] = []
    for phrase, resolved in _ago_hits(text, anchor):
        if "week" not in phrase:
            continue
        offsets = range(-NEIGHBOURHOOD_DAYS, NEIGHBOURHOOD_DAYS + 1)
        around.extend((resolved + timedelta(days=offset)).isoformat() for offset in offsets)
    return around


def window(text: str, anchor: date) -> tuple[str, str] | None:
    """The stretch of days a question's relative expressions cover.

    None when the text names no date; otherwise the earliest and latest ISO
    days. A point is widened three days each side, so "last weekend" reaches the
    entries of that weekend and "four weeks ago" the week around it. A stretch
    the user named is already exact and is taken as it is: "last week" is that
    Monday to that Sunday.
    """
    ends = _point_ends(text, anchor) + list(spans(text, anchor).values())
    if not ends:
        return None
    return min(first for first, _last in ends), max(last for _first, last in ends)


def _point_ends(text: str, anchor: date) -> list[tuple[str, str]]:
    """Each single day the text resolves, as its own neighbourhood of days."""
    out = timedelta(days=NEIGHBOURHOOD_DAYS)
    return [
        ((date.fromisoformat(value) - out).isoformat(), (date.fromisoformat(value) + out).isoformat())
        for value in resolutions(text, anchor).values()
    ]
