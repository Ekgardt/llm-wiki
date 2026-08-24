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

# What the page *is*, as a second factor on the same score. A status log is
# derived commentary; a decision page is the thing it comments on. Measured on
# this vault, authority alone could not tell them apart: the register outranked
# every decision page it discusses, because it offers seventy-one chunks of
# same-language text against a page's seven.
#
# Nothing is demoted below neutral except a gap stub, whose content is "this is
# not written yet". This vault answers code questions too, and a page under
# `scripts/` must still win when it is what was asked for — the prior lifts
# curated knowledge rather than pushing code down. The factor multiplies the
# fused score once per candidate, at query time: an index-time boost multiplies
# per matching term, which is why Lucene deprecated them. See
# docs/research/2026-08-24-ranking-by-what-a-page-is.md.
TYPE_WEIGHTS: dict[str, float] = {
    "decision": 1.25,
    "synthesis": 1.15,
    "concept": 1.15,
    "pattern": 1.10,
    "workflow": 1.10,
    "qa": 1.10,
    "entity": 1.05,
    "debugging": 1.05,
    "skill": 1.05,
    "rule": 1.05,
    "gap": 0.8,
}

DEFAULT_TYPE_WEIGHT = 1.0


def authority_weight(value: object) -> float:
    """Weight for one `source_authority` value; unknown or absent means 1.0."""
    if not isinstance(value, str):
        return DEFAULT_AUTHORITY_WEIGHT
    return AUTHORITY_WEIGHTS.get(value.strip().lower(), DEFAULT_AUTHORITY_WEIGHT)


def type_weight(value: object) -> float:
    """Weight for one page `type`; unknown or absent means 1.0."""
    if not isinstance(value, str):
        return DEFAULT_TYPE_WEIGHT
    return TYPE_WEIGHTS.get(value.strip().lower(), DEFAULT_TYPE_WEIGHT)


def trust_weight(authority: object, page_type: object) -> float:
    """Both factors, applied once: who said it, and what the page is."""
    return authority_weight(authority) * type_weight(page_type)
