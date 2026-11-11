"""Original pure provenance behavior tests, moved without changing assertions."""
from __future__ import annotations

import provenance
import pytest
from provenance import AUTHORITY_WEIGHTS, authority_weight

from tests.modules.provenance.fixtures import ANNOTATED, PROSE, RELATED


def test_an_unstated_page_ranks_as_inferred_and_other_sources_stay_neutral() -> None:
    weights = [
        authority_weight(None, "knowledge/notes/a-page.md"),
        authority_weight("user", "knowledge/notes/a-page.md"),
        authority_weight(None, "scripts/tool.py"),
        authority_weight(None, "knowledge/daily/2026-09-01.md"),
    ]

    assert weights == [AUTHORITY_WEIGHTS["inferred"], AUTHORITY_WEIGHTS["user"], 1.0, 1.0]


def test_a_session_record_ranks_below_a_compiled_page() -> None:
    """The words are the user's; the page written from them still wins.

    A session record is evidence, not a stated fact, so its authority weight sits
    below both a user-stated page and an ai-derived compiled one.
    """
    from provenance import authority_weight

    assert authority_weight("session", "knowledge/notes/x.md") < authority_weight("ai-derived", "knowledge/notes/x.md")
    assert authority_weight("session", "knowledge/notes/x.md") < authority_weight("user", "knowledge/notes/x.md")


def test_the_prior_is_only_ever_turned_off_by_a_positive_reading():
    """An unreadable or absent intent list keeps the vault's own rule."""
    assert provenance.curated_pages_first(()) is True
    assert provenance.curated_pages_first(None) is True
    assert provenance.curated_pages_first(("question", "cross_language")) is True
    assert provenance.curated_pages_first(("exact_identifier",)) is False


def test_living_under_a_code_root_never_lifts_a_page():
    """Raw evidence stays at its own weight; the rule only ever demotes."""
    raw = provenance.source_type_weight(
        "raw-source", "docs/notes/transcript.md", curated_first=True
    )
    assert raw == provenance.TYPE_WEIGHTS["raw-source"]
    assert raw < provenance.TYPE_WEIGHTS["doc"]


@pytest.mark.parametrize(
    ("content", "navigation"),
    [(RELATED, True), (ANNOTATED, True), ("## Related\n", True), (PROSE, False),
     ("- [[a]] is one option.\nThe other is prose.\n", False), (None, False), ("", False)],
)
def test_substance_weight_reads_link_density(content, navigation) -> None:
    from provenance import NAVIGATION_WEIGHT, substance_weight

    assert (substance_weight(content) == NAVIGATION_WEIGHT) is navigation
