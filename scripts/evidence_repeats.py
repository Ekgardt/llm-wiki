"""A passage said twice in one session reaches the reader once.

A user who restates a message ("I'll start by saying that I'm a backend
developer ... at NovaTech for 4 years and 3 months") had both copies retrieved,
and the second spent the reader's window on nothing new. A passage is a repeat
when most of its three-word shingles already occurred earlier in its own entry,
the resemblance-and-containment measure of Broder (1997) at the 0.8 threshold
near-duplicate removal commonly uses (Lee et al. 2022), every figure it names
was already said there, and it changes nothing of the earlier passage it
restates: aligned word by word against that passage (Ratcliff/Obershelp, as
`difflib` implements it), it replaces none of its words. Words it only adds
("As I said before, ...", or a role marker the transcript puts before it) are
framing; a replaced word is a correction, whether a figure in digits ("$500" to
"$900"), one in words ("four years" to "five years") or a name, and the passage
carrying it stays. Only the
same entry is compared: the same words in another session carry another date,
and the later one may be the current value.
"""
from __future__ import annotations

import re
from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher

SHINGLE_WORDS = 3
REPEATED_SHARE = 0.8
_WORD = re.compile(r"\w+")


@dataclass
class _Said:
    shingles: set[tuple[str, ...]] = field(default_factory=set)
    figures: set[str] = field(default_factory=set)
    passages: list[list[str]] = field(default_factory=list)


@dataclass(frozen=True)
class _Passage:
    words: list[str]
    shingles: set[tuple[str, ...]]
    figures: set[str]

    @classmethod
    def of(cls, text: str) -> _Passage:
        words = _WORD.findall(text.casefold())
        return cls(words, _shingles_of(words), _figures_of(words))


def _shingles_of(words: list[str]) -> set[tuple[str, ...]]:
    return {tuple(words[at : at + SHINGLE_WORDS]) for at in range(len(words) - SHINGLE_WORDS + 1)}


def _figures_of(words: list[str]) -> set[str]:
    return {word for word in words if any(c.isdigit() for c in word)}


def _closest(found: _Passage, held: _Said) -> list[str]:
    return max(held.passages, key=lambda words: len(found.shingles & _shingles_of(words)))


def _changes(earlier: list[str], found: list[str]) -> bool:
    """Whether `found` replaces a word of `earlier`; words it only adds are framing."""
    matcher = SequenceMatcher(None, earlier, found, autojunk=False)
    return any(tag == "replace" for tag, *_ in matcher.get_opcodes())


def _is_repeat(found: _Passage, held: _Said) -> bool:
    if not found.shingles or not found.figures <= held.figures:
        return False
    if len(found.shingles & held.shingles) < REPEATED_SHARE * len(found.shingles):
        return False
    return not _changes(_closest(found, held), found.words)


def repeated(passages: Sequence[tuple[Hashable, str]]) -> set[int]:
    """Positions of passages an earlier passage of the same entry already says."""
    held: dict[Hashable, _Said] = {}
    found: set[int] = set()
    for position, (entry, text) in enumerate(passages):
        said = _Passage.of(text)
        seen = held.setdefault(entry, _Said())
        if _is_repeat(said, seen):
            found.add(position)
            continue
        seen.shingles |= said.shingles
        seen.figures |= said.figures
        seen.passages.append(said.words)
    return found
