"""Code, not the model, does the counting and the arithmetic a claim declares.

The model reads well and computes badly, which is the program-aided reasoning
finding (PAL, arXiv:2211.10435; Program of Thoughts, arXiv:2211.12588): let
the model state the inputs and let code compute. The answer schema already
asks a count or a sum to list each counted item or summed figure in `inputs`,
and `aggregation_pass.COUNTING_RULE` says the count is the number of entries;
until now nothing computed it, so a claim could list four items and say three.

A claim that declares `count`, `sum` or a `difference` between two quantities
gets `computed`, the figure its own inputs give, and `computed_stated`,
whether its text already carries that figure. A difference between dates is
the calendar's (`calendar_pass`), so an input holding an ISO date is left to
it. Inputs this cannot read — a summed entry with no figure or with several,
durations in units that do not convert — give no figure rather than a guess.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

_ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(year|month|week|day)s?\b", re.IGNORECASE)
_UNITS = {"year": ("months", 12), "month": ("months", 1), "week": ("days", 7), "day": ("days", 1)}
_NUMBER_WORDS = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
    "nineteen", "twenty",
]
_WORD = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", re.IGNORECASE)


@dataclass(frozen=True)
class Tally:
    """A computed figure, and the sets of numbers any one of which states it."""

    figure: str
    stated_by: tuple[frozenset[str], ...]


def _shown(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _figures(text: str) -> list[Decimal]:
    """Every number in the text, dates removed, thousands separators ignored."""
    found = _NUMBER.findall(_ISO.sub(" ", text))
    return [Decimal(item.replace(",", "")) for item in found]


def _stated_numbers(text: str) -> set[str]:
    words = {str(_NUMBER_WORDS.index(word.lower())) for word in _WORD.findall(text)}
    return {_shown(value) for value in _figures(text)} | words


def _folded(inputs: Sequence[str]) -> set[str]:
    return {" ".join(item.casefold().split()) for item in inputs}


def count_of(inputs: Sequence[str]) -> Tally | None:
    """The number of distinct entries; an entry listed twice is one instance."""
    distinct = _folded(inputs)
    if not distinct:
        return None
    figure = str(len(distinct))
    return Tally(figure, (frozenset({figure}),))


def sum_of(inputs: Sequence[str]) -> Tally | None:
    """The total, when every entry carries exactly one figure."""
    figures = _one_figure_each(inputs)
    if len(figures) < 2:
        return None
    figure = _shown(sum(figures, Decimal(0)))
    return Tally(figure, (frozenset({figure}),))


def _one_figure_each(inputs: Sequence[str]) -> list[Decimal]:
    """One figure per entry, or nothing when any entry has none or several."""
    figures = [_figures(item) for item in inputs]
    if any(len(found) != 1 for found in figures):
        return []
    return [found[0] for found in figures]


def _duration(text: str) -> tuple[str, Decimal] | None:
    """A duration in one base unit — months or days — or a plain number."""
    parts = _parts(text)
    if not parts:
        return _plain(text)
    bases = {base for base, _amount in parts}
    if len(bases) != 1:
        return None
    return bases.pop(), sum((amount for _base, amount in parts), Decimal(0))


def _parts(text: str) -> list[tuple[str, Decimal]]:
    """Each stated amount converted to its base unit: years to months, weeks to days."""
    found = ((_UNITS[unit.lower()], Decimal(amount)) for amount, unit in _DURATION.findall(text))
    return [(base, scale * amount) for (base, scale), amount in found]


def _plain(text: str) -> tuple[str, Decimal] | None:
    figures = _figures(text)
    if len(figures) != 1:
        return None
    return "", figures[0]


def _quantity(text: str) -> tuple[str, Decimal] | None:
    if _ISO.search(text):
        return None
    try:
        return _duration(text)
    except InvalidOperation:
        return None


def _in_years(months: Decimal) -> Tally:
    years, rest = divmod(months, 12)
    figure = f"{_shown(years)} years and {_shown(rest)} months ({_shown(months)} months)"
    whole = frozenset({_shown(years), _shown(rest)} - {"0"})
    return Tally(figure, (whole, frozenset({_shown(months)})))


def _as_figure(base: str, value: Decimal) -> Tally:
    if base == "months":
        return _in_years(value)
    figure = " ".join(part for part in (_shown(value), base) if part)
    return Tally(figure, (frozenset({_shown(value)}),))


def difference_of(inputs: Sequence[str]) -> Tally | None:
    """The gap between two quantities of one kind; dates are the calendar's."""
    known = list(filter(None, (_quantity(item) for item in inputs)))
    if len(inputs) != 2 or len(known) != 2:
        return None
    (first_base, first), (second_base, second) = known
    if first_base != second_base:
        return None
    return _as_figure(first_base, abs(first - second))


COMPUTE: Mapping[str, Callable[[Sequence[str]], Tally | None]] = {
    "count": count_of,
    "sum": sum_of,
    "difference": difference_of,
}


def tally(claim: Mapping[str, Any]) -> Tally | None:
    """The figure the claim's own inputs give, or None when code cannot read them."""
    compute = COMPUTE.get(str(claim.get("derivation")))
    if compute is None:
        return None
    return compute([str(item).strip() for item in claim.get("inputs") or ()])


def states(claim: Mapping[str, Any], computed: Tally) -> bool:
    """Whether the claim's text already carries the computed figure."""
    numbers = _stated_numbers(str(claim.get("text") or ""))
    return any(expected <= numbers for expected in computed.stated_by)


def with_computed(answer: Mapping[str, Any]) -> None:
    """Mark every claim whose figure code can compute, in place."""
    for claim in answer.get("claims") or ():
        _mark(claim)


def _mark(claim: dict[str, Any]) -> None:
    computed = tally(claim)
    if computed is None:
        return
    claim["computed"] = computed.figure
    claim["computed_stated"] = states(claim, computed)
