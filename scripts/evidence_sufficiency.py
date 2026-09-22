"""Whether the shown evidence covers the question — a signal beside the model's confidence.

Confidence in an answer "is nearly blind to whether the question is answerable"
(Two Axes of LLM Abstention, 2026). The reader's refusal is one scale, and the
recorded run of 2026-09-18 shows what that costs: 26 refusals on answerable
questions, every one refused twice, most with the evidence in the prompt and a
reason that reads the evidence correctly and then declines it over a date four
days off or a word the question did not use.

So the second signal is ours and deterministic. A question has aspects: its
content terms, its figures, and the stretch of days its own relative expressions
resolve to. Each aspect is covered or not by the spans the model was shown; a
date aspect is covered when a shown span is dated within `DATE_SCOPE_DAYS` of
the question's window. The score is the covered share. It costs no token and
reads no benchmark label — the question's text and the evidence, nothing else.

It decides one thing. When the model refused and the evidence already covers
the question at or above `SUFFICIENT_TO_READ_AGAIN`, the remedy is not another
search — the evidence is in hand — but one more reading with the coverage
stated as data beside the question. Below it, the refusal look searches, as it
has since 2026-09-08. Both thresholds were set by Neyman–Pearson on the tune
half of the recorded run — fix the share of unanswerable twins the look may
fire on, take the threshold with the most coverage — and are frozen here; the
decide half is reported, never tuned on. The numbers and the stand-in they
were measured with are in
`docs/research/2026-09-22-quote-then-answer-and-a-refusal-calibrated-on-twins.md`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

# The share of unanswerable twins the read-again look may fire on, fixed before
# any threshold was looked at (Neyman–Pearson's α; Geifman & El-Yaniv's risk).
FALSE_FIRE_RATE = 0.10
# Set on the tune half of the 2026-09-18 run (231 originals, 231 twins): the
# smallest score that fires on at most α of the twins is 9/13, at which the
# look fires on 0.39 of the originals and 0.06 of the twins (d′ 1.27); on the
# decide half, untouched, 0.40 and 0.08 (d′ 1.13). Frozen here, rounded down.
SUFFICIENT_TO_READ_AGAIN = 0.69
# Not calibrated: only 7 tune and 13 decide questions resolve to a window at
# all, too few to set a tolerance on. This is the product's own three days
# (`temporal_anchor.NEIGHBOURHOOD_DAYS`), kept, and the note says so.
DATE_SCOPE_DAYS = 3

# A word that begins with a letter; figures are aspects of their own.
_WORD = re.compile(r"[^\W\d_][\w'-]*", re.UNICODE)
_FIGURE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*(?!\w)(?!\.\d)")
_ISO_DAY = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_MIN_TERM_CHARS = 3
# Plain English plurals, longest ending first, so "glass" survives "s".
_PLURALS = (("ies", "y"), ("sses", "ss"), ("ss", "ss"), ("s", ""))
# The words a question is built from rather than about. Not an aspect: a span
# that says "which" or "remind" has covered nothing. Time words are here too,
# because the stretch they name is the date aspect, resolved by the calendar.
_SCAFFOLD = frozenset(
    """
    a an the and or but of to in on at for from by with about into over after before
    is are was were be been being am do does did done have has had having will would
    can could should shall may might must i me my mine we our us you your he she it
    its they them their this that these those what which who whom whose when where
    why how many much often long ago last next past previous earlier later again
    still also just only ever never any some all every each both few more most other
    another such same very remind tell say said told mention mentioned conversation
    chat discussion looking back going through remember recall confirm wondering
    wanted asked ask know please there here then than so if not no yes name kind
    type sort one two three four five six seven eight nine ten first second third
    day days week weeks weekend weekends month months year years today yesterday
    tomorrow time date ago recently current currently
    что как когда где сколько какой какая какое какие кто чем это был была было
    были есть мне мой моя моё мои нас наш ты вы он она они его её их
    """.split()
)


def _stem(word: str) -> str:
    """The word without a possessive or a plain English plural ending."""
    lowered = word.casefold().strip("'-").removesuffix("'s")
    for ending, replacement in _PLURALS:
        if lowered.endswith(ending) and len(lowered) > len(ending) + 1:
            return lowered[: -len(ending)] + replacement
    return lowered


def _is_term(word: str) -> bool:
    return len(word) >= _MIN_TERM_CHARS and word.casefold() not in _SCAFFOLD


def terms_of(text: str) -> frozenset[str]:
    """The stemmed content words of the text, question scaffolding removed."""
    return frozenset(_stem(word) for word in _WORD.findall(str(text)) if _is_term(word))


def figures_of(text: str) -> frozenset[str]:
    return frozenset(_FIGURE.findall(str(text)))


def _parsed_day(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def days_of(text: str, path: str = "") -> frozenset[date]:
    """Every ISO day the span states, and the day its file is named after.

    A daily entry is `knowledge/daily/2023-05-26.md`: the file name is the day
    the session was captured, and the manifest hands the model the path.
    """
    found = (_parsed_day(match) for match in _ISO_DAY.findall(str(text) + " " + str(path)))
    return frozenset(day for day in found if day is not None)


def question_window(question: str, asked_on: date) -> tuple[date, date] | None:
    """The stretch of days the question's own relative expressions cover."""
    from temporal_anchor import window

    span = window(question, asked_on)
    if span is None:
        return None
    return date.fromisoformat(span[0]), date.fromisoformat(span[1])


def _distance(window: tuple[date, date], day: date) -> int:
    first, last = window
    if day < first:
        return (first - day).days
    if day > last:
        return (day - last).days
    return 0


def days_from_window(window: tuple[date, date], days: Iterable[date]) -> int | None:
    """How far the nearest of these days lies from the window; None with no day at all."""
    distances = [_distance(window, day) for day in days]
    if not distances:
        return None
    return min(distances)


@dataclass(frozen=True)
class Sufficiency:
    """Which of the question's aspects the shown evidence covers."""

    covered: tuple[str, ...]
    missing: tuple[str, ...]
    # The nearest shown day's distance from the question's window, in days;
    # None when the question names no stretch of days or no span is dated.
    days_from_window: int | None = None

    @property
    def score(self) -> float | None:
        """The covered share, or None when the question has no aspect to cover."""
        total = len(self.covered) + len(self.missing)
        if not total:
            return None
        return len(self.covered) / total

    def as_record(self) -> dict[str, object]:
        return {
            "score": self.score,
            "covered": list(self.covered),
            "missing": list(self.missing),
            "days_from_window": self.days_from_window,
        }


def _within_scope(distance: int | None) -> bool:
    return distance is not None and distance <= DATE_SCOPE_DAYS


def _date_aspect(
    window: tuple[date, date] | None, span: object | None
) -> tuple[list[str], list[str], int | None]:
    """The date aspect of one span as covered or missing, and its distance from the window."""
    if window is None:
        return [], [], None
    name = f"days {window[0].isoformat()}..{window[1].isoformat()}"
    distance = _span_distance(window, span)
    if _within_scope(distance):
        return [name], [], distance
    return [], [name], distance


def _span_distance(window: tuple[date, date], span: object | None) -> int | None:
    if span is None:
        return None
    return days_from_window(window, days_of(span.text, span.relative_path))


def _stated(span: object | None) -> frozenset[str]:
    """Every term and figure one span states."""
    if span is None:
        return frozenset()
    return terms_of(span.text) | figures_of(span.text)


def _wanted(question: str) -> list[str]:
    """The question's terms and figures, in a stable order."""
    return sorted(terms_of(question)) + sorted(figures_of(question))


def _span_reading(wanted: list[str], window: tuple[date, date] | None, span: object | None) -> Sufficiency:
    """What one span covers of the question; `None` is the reading with no span at all."""
    seen = _stated(span)
    covered = [aspect for aspect in wanted if aspect in seen]
    missing = [aspect for aspect in wanted if aspect not in seen]
    date_covered, date_missing, distance = _date_aspect(window, span)
    return Sufficiency(tuple(covered + date_covered), tuple(missing + date_missing), distance)


def sufficiency(question: str, spans: Sequence[object], asked_on: date) -> Sufficiency:
    """How much of the question the best single shown span covers.

    Each span carries `text` and `relative_path`, as `GroundedEvidence` does.
    The unit is one span, not the union of all of them: measured on the
    recorded run, a twin's twelve topical distractors state every word of the
    question between them, while one span that states the terms, the figures
    and a day within `DATE_SCOPE_DAYS` of the window together is what an
    original has and a twin lacks. The union could hold no false-fire rate at
    any threshold; the single span holds α = 0.10. See the research note.
    """
    wanted = _wanted(question)
    window = question_window(question, asked_on)
    readings = [_span_reading(wanted, window, span) for span in spans]
    return max(readings, key=lambda reading: len(reading.covered), default=_span_reading(wanted, window, None))


def reads_again(measured: Sufficiency) -> bool:
    """Whether a refusal on this evidence is read again rather than searched for."""
    score = measured.score
    return score is not None and score >= SUFFICIENT_TO_READ_AGAIN


def _window_line(measured: Sufficiency) -> str:
    if measured.days_from_window is None:
        return ""
    return (
        f"The nearest dated span is {measured.days_from_window} day(s) from the days the "
        "question asks about; a relative time is approximate, and evidence within "
        f"{DATE_SCOPE_DAYS} days of it is inside the requested scope.\n"
    )


def coverage_note(measured: Sufficiency) -> str:
    """What the evidence covers, stated as data beside the question for a second reading."""
    covered = ", ".join(measured.covered) or "(nothing)"
    missing = ", ".join(measured.missing) or "(nothing)"
    return (
        "<evidence_coverage>\n"
        "Measured by the system, not by you: the evidence shown states these terms, "
        f"figures and days of the question: {covered}. Not stated: {missing}.\n"
        + _window_line(measured)
        + "Read the evidence once more and answer from it when it carries the answer; "
        "abstain only for a gap this coverage does not close.\n"
        "</evidence_coverage>\n"
    )
