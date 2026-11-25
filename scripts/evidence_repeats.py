"""A passage said twice in one session reaches the reader once.

A user who restates a message ("I'll start by saying that I'm a backend
developer ... at NovaTech for 4 years and 3 months") had both copies retrieved,
and the second spent the reader's window on nothing new. A passage is a repeat
when most of its three-word shingles already occurred earlier in its own entry:
the resemblance-and-containment measure of Broder (1997), at the 0.8 threshold
near-duplicate removal commonly uses (Lee et al. 2022). Only the same entry is
compared: the same words in another session carry another date, and the later
one may be the current value.
"""
from __future__ import annotations

import re
from collections.abc import Hashable, Sequence

SHINGLE_WORDS = 3
REPEATED_SHARE = 0.8
_WORD = re.compile(r"\w+")


def shingles(text: str) -> set[tuple[str, ...]]:
    words = _WORD.findall(text.casefold())
    return {tuple(words[at : at + SHINGLE_WORDS]) for at in range(len(words) - SHINGLE_WORDS + 1)}


def _is_repeat(found: set[tuple[str, ...]], held: set[tuple[str, ...]]) -> bool:
    if not found:
        return False
    return len(found & held) >= REPEATED_SHARE * len(found)


def repeated(passages: Sequence[tuple[Hashable, str]]) -> set[int]:
    """Positions of passages an earlier passage of the same entry already says."""
    held: dict[Hashable, set[tuple[str, ...]]] = {}
    found: set[int] = set()
    for position, (entry, text) in enumerate(passages):
        words = shingles(text)
        seen = held.setdefault(entry, set())
        if _is_repeat(words, seen):
            found.add(position)
            continue
        seen |= words
    return found
