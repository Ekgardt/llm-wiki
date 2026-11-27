"""A passage said twice in one session reaches the reader once.

A user who restates a message ("I'll start by saying that I'm a backend
developer ... at NovaTech for 4 years and 3 months") had both copies retrieved,
and the second spent the reader's window on nothing new. A passage is a repeat
when most of its three-word shingles already occurred earlier in its own entry,
the resemblance-and-containment measure of Broder (1997) at the 0.8 threshold
near-duplicate removal commonly uses (Lee et al. 2022), and every figure it
names was already said there: a restatement that corrects one figure ("$500"
to "$900") carries the new value and stays. Only the same entry is compared:
the same words in another session carry another date, and the later one may be
the current value.
"""
from __future__ import annotations

import re
from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field

SHINGLE_WORDS = 3
REPEATED_SHARE = 0.8
_WORD = re.compile(r"\w+")


@dataclass
class _Said:
    shingles: set[tuple[str, ...]] = field(default_factory=set)
    figures: set[str] = field(default_factory=set)


def shingles(text: str) -> set[tuple[str, ...]]:
    words = _WORD.findall(text.casefold())
    return {tuple(words[at : at + SHINGLE_WORDS]) for at in range(len(words) - SHINGLE_WORDS + 1)}


def figures(text: str) -> set[str]:
    return {word for word in _WORD.findall(text.casefold()) if any(c.isdigit() for c in word)}


def _is_repeat(found: _Said, held: _Said) -> bool:
    if not found.shingles or not found.figures <= held.figures:
        return False
    return len(found.shingles & held.shingles) >= REPEATED_SHARE * len(found.shingles)


def repeated(passages: Sequence[tuple[Hashable, str]]) -> set[int]:
    """Positions of passages an earlier passage of the same entry already says."""
    held: dict[Hashable, _Said] = {}
    found: set[int] = set()
    for position, (entry, text) in enumerate(passages):
        said = _Said(shingles(text), figures(text))
        seen = held.setdefault(entry, _Said())
        if _is_repeat(said, seen):
            found.add(position)
            continue
        seen.shingles |= said.shingles
        seen.figures |= said.figures
    return found
