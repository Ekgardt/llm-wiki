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
    # Raw session evidence: kept because it holds what exists nowhere else, and
    # ranked below every curated page because there are hundreds of them, each up
    # to half a megabyte of the same conversations the pages were compiled from.
    # Measured: importing 236 past sessions at neutral weight took the vault
    # stand from hit@5 0.7 to 0.0 — every place taken by a transcript of the
    # discussion instead of the decision it produced.
    "raw-source": 0.6,
    # Prose under a code root — research notes, status registers, design
    # write-ups. It is commentary on the decisions, and the vault's own rule is
    # to answer from the compiled pages first and read the commentary after.
    # Measured: with it at neutral, the audit register was the first result on
    # all ten stand questions and hit@5 fell from 0.7 to 0.4.
    "doc": 0.8,
}

DEFAULT_TYPE_WEIGHT = 1.0

# What the question asks for decides whether the vault's own retrieval rule —
# answer from the compiled pages, read the commentary after — applies at all.
# These intents say the question is about code: it names a path, a filename or a
# symbol, or it asks about dependencies, structure, or impact. For those a file
# that lives with the code is the answer, not commentary on someone's decision.
CODE_SHAPED_INTENTS = frozenset(
    {"exact_identifier", "graph_relation", "repo_map", "impact"}
)


def curated_pages_first(intents: object) -> bool:
    """Whether "answer from the compiled pages first" applies to this question.

    Detection turns the prior *off*, never on: a pre-retrieval routing mistake
    is not recovered downstream, so a query this cannot read keeps ranking
    exactly as it does today. See
    `docs/research/2026-08-25-intent-conditional-ranking-weights.md`.
    """
    if not isinstance(intents, (tuple, list, set, frozenset)):
        return True
    return not CODE_SHAPED_INTENTS.intersection(str(item) for item in intents)


def _code_roots() -> frozenset[str]:
    """The one list of code roots, read where the corpus already declares it."""
    from corpus_snapshot import APPROVED_CODE_ROOTS

    return APPROVED_CODE_ROOTS


def _under_code_root(relative_path: object) -> bool:
    if not isinstance(relative_path, str) or not relative_path:
        return False
    head = relative_path.replace("\\", "/").lstrip("/").split("/", 1)[0]
    return head in _code_roots()


def source_type_weight(
    page_type: object, relative_path: object, *, curated_first: bool
) -> float:
    """What the page is — and, for a knowledge question, where it lives.

    The declared type is not enough on its own. Measured on this vault: a design
    spec under `docs/` declares `type: decision` and then outranks the decision
    page in `knowledge/notes/` it comments on, and a question sheet under
    `benchmark/` is typed `code` and outranks everything. Under the curated-first
    prior a source that lives with the code takes the commentary weight this
    table already names, and nothing is ever lifted by living there.
    """
    if curated_first and _under_code_root(relative_path):
        return min(type_weight(page_type), TYPE_WEIGHTS["doc"])
    return type_weight(page_type)


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
