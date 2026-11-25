"""Code, not the model, does the counting and the arithmetic a claim declares.

The model reads well and computes badly, which is the program-aided reasoning
finding (PAL, arXiv:2211.10435; Program of Thoughts, arXiv:2211.12588): let
the model state the inputs and let code compute. The answer schema already
asks a count or a sum to list each counted item or summed figure in `inputs`,
and `aggregation_pass.COUNTING_RULE` says the count is the number of entries;
until now nothing computed it, so a claim could list four items and say three.

A claim that declares `count`, `sum` or a `difference` between quantities of
one kind gets `computed`, the figure its own inputs give, and
`computed_stated`, whether its text states that figure as a whole. A
difference between dates is the calendar's (`calendar_pass`), so an input
naming a date is left to it. Inputs this cannot read — an entry with no figure
or with several, quantities of different units, a figure too large to compute
exactly — give no figure rather than a guess.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, Inexact, InvalidOperation, localcontext
from typing import Any

from calendar_pass import _ISO

# Counts in the benchmark's answers are spelled out at most up to twenty; a
# larger count written in words is reported as not stated, never miscounted.
_NUMBER_WORDS = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
    "nineteen", "twenty",
]
_SPELLED = "|".join(_NUMBER_WORDS)
_WORD = re.compile(rf"\b({_SPELLED})\b", re.IGNORECASE)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_DURATION = re.compile(
    rf"(?<![\w.])(\d+(?:\.\d+)?|{_SPELLED})\s*(year|month|week|day)s?\b", re.IGNORECASE
)
# A rate ("$15 a month", "3 times a week") or a fraction ("a year and a half")
# names an amount this does not read, so the entry gives no figure at all.
_UNREAD = re.compile(r"\b(?:half|times|per|an?\s+(?:year|month|week|day))\b", re.IGNORECASE)
_UNITS = {"year": ("months", 12), "month": ("months", 1), "week": ("days", 7), "day": ("days", 1)}
_PLURAL = re.compile(r"\b(year|month|week|day)s\b")
_AMOUNT = re.compile(r"([$€£¥₽])?\s*\d[\d,]*(?:\.\d+)?\s*([^\W\d_]*)")
_MONTH_NAME = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|november"
    r"|december)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Tally:
    """A computed figure, and the patterns any one of which states it as a whole."""

    figure: str
    stated_by: tuple[str, ...]


def _shown(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _figures(text: str) -> list[Decimal]:
    """Every number in the text, dates removed, thousands separators ignored."""
    found = _NUMBER.findall(_ISO.sub(" ", text))
    return [Decimal(item.replace(",", "")) for item in found]


def _folded(inputs: Sequence[str]) -> set[str]:
    return {" ".join(item.casefold().split()) for item in inputs}


def _plain_tally(value: Decimal) -> Tally:
    figure = _shown(value)
    return Tally(figure, (re.escape(figure),))


def count_of(inputs: Sequence[str]) -> Tally | None:
    """The number of distinct entries; an entry listed twice is one instance."""
    distinct = _folded(inputs)
    if not distinct:
        return None
    return _plain_tally(Decimal(len(distinct)))


def _amount(word: str) -> Decimal:
    lowered = word.lower()
    if lowered in _NUMBER_WORDS:
        return Decimal(_NUMBER_WORDS.index(lowered))
    return Decimal(word)


def _parts(text: str) -> list[tuple[str, Decimal]]:
    """Each stated amount converted to its base unit: years to months, weeks to days."""
    found = ((_UNITS[unit.lower()], _amount(amount)) for amount, unit in _DURATION.findall(text))
    return [(base, scale * amount) for (base, scale), amount in found]


def _plain(text: str) -> tuple[str, Decimal] | None:
    """One figure and its unit: the currency before it, else the word after it."""
    figures = _figures(text)
    if len(figures) != 1:
        return None
    return f"unit:{_unit(text)}", figures[0]


def _unit(text: str) -> str:
    match = _AMOUNT.search(text)
    if match is None:
        return ""
    return match.group(1) or match.group(2).lower().rstrip("s")


def _duration(text: str) -> tuple[str, Decimal] | None:
    """A duration in one base unit — months or days — or a plain figure with its unit."""
    parts = _parts(text)
    if not parts:
        return _plain(text)
    bases = {base for base, _amount in parts}
    if len(bases) != 1:
        return None
    return bases.pop(), sum((amount for _base, amount in parts), Decimal(0))


def _quantity(text: str) -> tuple[str, Decimal] | None:
    if _ISO.search(text) or _MONTH_NAME.search(text) or _UNREAD.search(text):
        return None
    return _duration(text)


def _same_kind(inputs: Sequence[str]) -> tuple[str, list[Decimal]] | None:
    """Every entry's amount when all of them are quantities of one unit."""
    read = [_quantity(item) for item in inputs]
    if None in read:
        return None
    return _one_base([quantity for quantity in read if quantity is not None])


def _one_base(known: list[tuple[str, Decimal]]) -> tuple[str, list[Decimal]] | None:
    bases = {base for base, _value in known}
    if len(bases) != 1:
        return None
    return bases.pop(), [value for _base, value in known]


def _in_years(months: Decimal) -> Tally:
    years, rest = divmod(months, 12)
    y, m, total = _shown(years), _shown(rest), _shown(months)
    patterns = [rf"{total} month", rf"{y} year,? (?:and )?{m} month"]
    patterns += [rf"{y} year(?!,? (?:and )?\d+ month)"] if rest == 0 else []
    return Tally(f"{y} years and {m} months ({total} months)", tuple(patterns))


def _in_days(days: Decimal) -> Tally:
    patterns = [rf"{_shown(days)} day"]
    patterns += [rf"{_shown(days / 7)} week(?!,? (?:and )?\d+ day)"] if days % 7 == 0 else []
    return Tally(f"{_shown(days)} days", tuple(patterns))


def _as_figure(base: str, value: Decimal) -> Tally:
    if base == "months":
        return _in_years(value)
    if base == "days":
        return _in_days(value)
    return _plain_tally(value)


def sum_of(inputs: Sequence[str]) -> Tally | None:
    """The total, when every entry carries one figure of the same unit."""
    kind = _same_kind(inputs) if len(inputs) >= 2 else None
    if kind is None:
        return None
    base, values = kind
    return _as_figure(base, sum(values, Decimal(0)))


def difference_of(inputs: Sequence[str]) -> Tally | None:
    """The gap between two quantities of one kind; dates are the calendar's."""
    kind = _same_kind(inputs) if len(inputs) == 2 else None
    if kind is None:
        return None
    base, (first, second) = kind
    return _as_figure(base, abs(first - second))


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
    inputs = [str(item).strip() for item in claim.get("inputs") or ()]
    try:
        with localcontext() as exact:
            exact.traps[Inexact] = True
            return compute(inputs)
    except (Inexact, InvalidOperation):
        return None


def _normalized(text: str) -> str:
    """The text with spelled numbers as digits, plain figures shown one way, units singular."""
    digits = _WORD.sub(lambda word: str(_NUMBER_WORDS.index(word.group(0).lower())), text)
    shown = _NUMBER.sub(lambda number: _shown(Decimal(number.group(0).replace(",", ""))), digits)
    return " ".join(_PLURAL.sub(r"\1", shown.casefold()).split())


def states(claim: Mapping[str, Any], computed: Tally) -> bool:
    """Whether the claim's text states the computed figure as a whole."""
    text = _normalized(str(claim.get("text") or ""))
    return any(
        re.search(rf"(?<![\w.]){pattern}(?!\w|\.\d)", text) for pattern in computed.stated_by
    )


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
