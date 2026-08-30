"""Under a budget, a repeat of a page goes before the only span from another.

Measured on this vault 2026-08-30 by the compiler's own trace: a median of two
of twelve retrieved spans survive `_fitted_selection`. Questions answerable
from one session are fine — those are the categories that work. A multi-session
question needs facts from two sessions and a temporal one a date from one and a
fact from another, and with two slots, spending both on the same page answers
neither. The answer text reached the model for 2 of 12 multi-session and 4 of
13 temporal questions.

Plain tail-shedding drops by rank alone. The 2026 work on budget-constrained
multi-hop retrieval reports that satisfying coverage first — the best span from
each distinct source, then the rest of the budget — is what recovers those
answers, while plain ranking and plain diversity each lose complementary
evidence. See `docs/research/2026-08-30-what-survives-into-context.md`.

These tests pin the shedding order, which is deterministic. Whether it moves
the benchmark is a separate, measured question.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import query_memory  # noqa: E402


@dataclass(frozen=True)
class _Chunk:
    id: str
    parent_page: str


def _chunks(*pairs: tuple[str, str]) -> list[_Chunk]:
    return [_Chunk(identifier, page) for identifier, page in pairs]


def _shed_until(kept: list, remaining: int) -> list:
    while len(kept) > remaining:
        query_memory._shed_one(kept)
    return kept


def test_a_repeat_of_a_page_goes_before_another_page() -> None:
    """Two slots, three spans: keep one from each page, not two from the first."""
    kept = _chunks(("a1", "A"), ("a2", "A"), ("b1", "B"))

    _shed_until(kept, 2)

    assert [chunk.id for chunk in kept] == ["a1", "b1"]


def test_the_repeat_dropped_is_the_last_one() -> None:
    """Within the rule the ranking still decides which repeat goes."""
    kept = _chunks(("a1", "A"), ("a2", "A"), ("a3", "A"))

    query_memory._shed_one(kept)

    assert [chunk.id for chunk in kept] == ["a1", "a2"]


def test_with_no_repeat_it_is_tail_shedding_exactly_as_before() -> None:
    """The old behaviour is preserved wherever the new rule does not apply."""
    kept = _chunks(("a1", "A"), ("b1", "B"), ("c1", "C"))

    query_memory._shed_one(kept)

    assert [chunk.id for chunk in kept] == ["a1", "b1"]


def test_coverage_is_kept_down_to_the_last_two_pages() -> None:
    kept = _chunks(("a1", "A"), ("a2", "A"), ("b1", "B"), ("b2", "B"), ("c1", "C"))

    _shed_until(kept, 3)

    assert {chunk.parent_page for chunk in kept} == {"A", "B", "C"}


def test_the_highest_ranked_span_is_never_the_first_to_go() -> None:
    """Rank one survives every trim; that is why single-session answers work."""
    kept = _chunks(("a1", "A"), ("a2", "A"), ("b1", "B"), ("c1", "C"))

    _shed_until(kept, 1)

    assert [chunk.id for chunk in kept] == ["a1"]


def test_one_span_is_left_alone() -> None:
    kept = _chunks(("a1", "A"))

    query_memory._shed_one(kept)

    assert kept == []


def test_the_redundant_index_finds_the_last_repeat() -> None:
    kept = _chunks(("a1", "A"), ("b1", "B"), ("a2", "A"), ("b2", "B"))

    assert query_memory._redundant_index(kept) == 3


def test_no_repeat_has_no_redundant_index() -> None:
    kept = _chunks(("a1", "A"), ("b1", "B"))

    assert query_memory._redundant_index(kept) is None
