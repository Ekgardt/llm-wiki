"""One trust contract: how typed provenance weighs on every retrieval path.

Hierarchy from CLAUDE.md rule 13: user-stated > web-sourced > ai-derived >
inferred. The table lived in `search_memory.py` and reached only the lexical
paths, so the hybrid path ranked a guess and a stated fact alike. Both import
it from here now, and the weight multiplies whichever score decides the order.
"""
from __future__ import annotations

# Higher weight = preferred in ranking (typed provenance).
AUTHORITY_WEIGHTS: dict[str, float] = {
    "user": 1.35,
    "human": 1.35,
    "web": 1.1,
    "ai-derived": 1.0,
    "ai": 1.0,
    "inferred": 0.8,
    "unknown": 1.0,
    # A session record holds the user's own words, but unreviewed and unedited:
    # it is evidence, not a stated fact, so a compiled page that was written from
    # it still outranks it. See session-evidence-retention-decision.
    "session": 0.9,
}

DEFAULT_AUTHORITY_WEIGHT = 1.0


def authority_weight(value: object) -> float:
    """Weight for one `source_authority` value; unknown or absent means 1.0."""
    if not isinstance(value, str):
        return DEFAULT_AUTHORITY_WEIGHT
    return AUTHORITY_WEIGHTS.get(value.strip().lower(), DEFAULT_AUTHORITY_WEIGHT)
