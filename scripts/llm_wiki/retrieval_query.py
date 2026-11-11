"""Retrieval profiles, weights, query analysis and the retrieval record types."""
from __future__ import annotations

import hashlib
import re
import time
from collections.abc import (
    Callable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from llm_wiki import runtime_roots


def normalized_filename_stem(value: str) -> str:
    name = Path(value.strip()).name.casefold()
    if name.endswith(".md"):
        name = name[:-3]
    return "-".join(part for part in re.split(r"[\s_-]+", name) if part)


def _first_exact_filename(
    candidates: Sequence[RetrievalCandidate], normalized: str
) -> RetrievalCandidate | None:
    for candidate in candidates:
        if normalized_filename_stem(candidate.relative_path) == normalized:
            return candidate
    return None


def promote_exact_filename(
    candidates: Sequence[RetrievalCandidate], query: str
) -> tuple[RetrievalCandidate, ...]:
    normalized = normalized_filename_stem(query)
    if not normalized:
        return tuple(candidates)
    exact = _first_exact_filename(candidates, normalized)
    if exact is None:
        return tuple(candidates)
    return (exact, *(candidate for candidate in candidates if candidate is not exact))

PROFILES = (
    "DIRECT",
    "EXACT",
    "BASE",
    "HYBRID",
    "GRAPH",
    "TEMPORAL",
    "REPO_MAP",
    "IMPACT",
    "GLOBAL",
    "CACHED_FULL",
)

# Declared signals each profile may request. Actual use is reported at runtime.
PROFILE_SIGNALS: dict[str, tuple[str, ...]] = {
    "DIRECT": ("lexical",),
    "EXACT": ("lexical",),
    "BASE": ("lexical",),
    "HYBRID": ("lexical", "dense"),
    "GRAPH": ("lexical", "graph"),
    "TEMPORAL": ("lexical",),
    "REPO_MAP": ("lexical", "graph"),
    "IMPACT": ("lexical", "graph"),
    "GLOBAL": ("lexical", "dense", "graph"),
    "CACHED_FULL": ("lexical",),
}

RRF_K = 60
# A short answer needs a long candidate pool: several chunks of one page are
# one answer repeated, and only a pool wider than the answer can hold the
# pages the page-diverse order then leads with.
CANDIDATE_FANOUT = 8
MIN_CANDIDATE_POOL = 40
MAX_CANDIDATE_POOL = 200
BM25_WEIGHT = 2.0
DENSE_WEIGHT = 1.0
GRAPH_WEIGHT = 0.5

# The graph lane walks one hop from its seeds (docs/research/2026-09-22-the-walk-has-nothing-to-
# walk.md): memory questions never route to it, so no measurement justifies more.
GRAPH_MAX_HOPS = 1
# The graph lane's shape: the top-ranked hits a one-hop walk starts from, the
# neighbours taken per seed and in all. Ranking parameters of the hybrid design, not
# cuts of results: the lane adds candidates beside the lexical and dense ones
# (docs/research/2026-09-22-the-walk-has-nothing-to-walk.md). Basis of the numbers
# unknown — review when a rank evaluation measures the lane's recall.
GRAPH_SEED_LIMIT = 5
GRAPH_PER_SEED_LIMIT = 4
GRAPH_GLOBAL_LIMIT = 12
GRAPH_EDGE_DECAY: dict[str, float] = {
    "BELONGS_TO_PROJECT": 0.65,
    "CALLS": 0.85,
    "CHECKPOINT_CHANGED_FILE": 0.75,
    "CHECKPOINT_EVIDENCED_BY_EVENT": 0.65,
    "CHECKPOINT_HAS_BLOCKER": 0.75,
    "CHECKPOINT_RECORDED_DECISION": 0.75,
    # `CO_CHANGED_WITH` was weighted here with no producer anywhere in the
    # product; its island was removed on 2026-09-17 (audit 3, C3). See
    # `docs/research/2026-09-17-graph-three-islands-nothing-sails-to.md`.
    "CONTAINS": 0.70,
    "DEFINES": 0.80,
    "EVIDENCED_BY": 0.70,
    "EXPOSES": 0.80,
    "IMPLEMENTS": 0.80,
    "IMPORTS": 0.80,
    "INHERITS": 0.80,
    "LINKS_TO": 0.65,
    "PROJECT_HAS_CHECKPOINT": 0.70,
    "READS": 0.75,
    "REFERENCES_SYMBOL": 0.75,
    "SUPERSEDES": 0.70,
    "WRITES": 0.75,
}
GRAPH_PROFILE_DIRECTIONS: dict[str, tuple[str, ...]] = {
    "GRAPH": ("in", "out"),
    "REPO_MAP": ("out",),
    "IMPACT": ("in",),
    "GLOBAL": ("in", "out"),
}
GRAPH_PROFILE_EDGE_TYPES: dict[str, tuple[str, ...]] = {
    "GRAPH": tuple(GRAPH_EDGE_DECAY),
    "REPO_MAP": (
        "CONTAINS",
        "DEFINES",
        "EXPOSES",
        "IMPLEMENTS",
        "IMPORTS",
        "INHERITS",
    ),
    # `CO_CHANGED_WITH` stood here too until its island was removed on
    # 2026-09-17 (audit 3, C3). Every name in these tuples has to be a key of
    # `GRAPH_EDGE_DECAY`, or the profile asks for a weight that is not there;
    # `tests/test_graph_retrieval.py` holds that line.
    "IMPACT": (
        "CALLS",
        "CHECKPOINT_CHANGED_FILE",
        "IMPORTS",
        "READS",
        "REFERENCES_SYMBOL",
        "WRITES",
    ),
    "GLOBAL": tuple(GRAPH_EDGE_DECAY),
}

BackendFn = Callable[..., Sequence[Mapping[str, Any]] | None]


class GenerationSealChanged(RuntimeError):
    """The active immutable generation changed during one retrieval."""

_QUOTE_RE = re.compile(r'"([^"\n]{1,256})"')
_PATH_RE = re.compile(
    r"(?P<path>"
    r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+"
    r"\.(?:md|py|ts|tsx|js|jsx|rs|go|java|cs|sqlite3?|json|yaml|yml|toml)"
    r")"
)
_FILENAME_RE = re.compile(
    r"(?<![A-Za-z0-9_/.-])"
    r"(?P<file>[A-Za-z0-9_.-]+\.(?:md|py|ts|tsx|js|jsx|rs|go|java|cs|sqlite3?|json|yaml|yml|toml))"
    r"(?![A-Za-z0-9_/.-])"
)
_SLUG_RE = re.compile(r"\b(?P<slug>[a-z0-9]+(?:-[a-z0-9]+){1,12})\b")
_SNAKE_RE = re.compile(r"\b(?P<snake>[a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")
_CAMEL_RE = re.compile(r"\b(?P<camel>[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b")
_QUESTION_RE = re.compile(
    r"(?:"
    r"^(?P<q_en>who|what|when|where|why|how|which|does|do|is|are|can|should)\b"
    r"|^(?P<q_ru>кто|что|когда|где|почему|зачем|как|какой|какая|какие|какое)\b"
    r"|^(?P<q_zh>谁|什么|何时|哪里|哪儿|为什么|怎么|如何|哪)"
    r"|[?？]"
    r")",
    re.IGNORECASE,
)
# What may follow a relational preposition and make it about time.
_WHEN = (
    r"(?:\d{4}(?:-\d{2}(?:-\d{2})?)?"
    r"|yesterday|today|tomorrow|now"
    r"|вчера|сегодня|завтра|сейчас)"
)
# A relational preposition is temporal only when something temporal follows it.
# `после` and `after` are ordinary sequence words, and reading them alone as a
# time window costs the answer: measured on this vault, "как устроен повтор
# после карантина" was routed to TEMPORAL, TEMPORAL declares no dense signal,
# so the grounded answer never asked the vectors. Retrieval returned six lexical
# rows — four of them the same status document, none of them the decision page
# the vault holds — and the provider honestly reported insufficient evidence.
# Every other alternative in this group was already anchored to a real time
# expression; these prepositions were the ones that were not.
_TEMPORAL_RE = re.compile(
    r"(?:"
    r"\b(?P<t_en_anchored>since|before|after|until|as of|from)\s+" + _WHEN + r"\b"
    r"|\b(?P<t_en>yesterday|today|last week|last month|last year"
    r"|in \d{4}|from \d{4}|between \d{4})\b"
    r"|(?P<t_ru_anchored>(?:с|со|после|до|от)\s+" + _WHEN + r")"
    r"|(?P<t_ru>вчера|сегодня|на этой неделе)"
    r"|(?P<t_zh>自\s*\d{4}|以来|自从)"
    r")",
    re.IGNORECASE,
)
_GRAPH_RE = re.compile(
    r"(?:"
    r"\b(?P<g_en>depends on|depended on|dependency|dependencies|calls|callers|"
    r"callees|related to|linked to|neighbors?|imports?|imported by)\b"
    r"|(?P<g_ru>зависит|зависимости|связан)"
    r"|(?P<g_zh>依赖|什么依赖|相关联)"
    r")",
    re.IGNORECASE,
)
_REPO_MAP_RE = re.compile(
    r"(?:"
    r"\b(?P<r_en>repo map|repository map|codebase map|architecture overview|"
    r"project structure|directory structure)\b"
    r"|(?P<r_ru>карт[ауи]\s+репозитори[яию]|структур[аыу]\s+проекта)"
    r"|(?P<r_zh>仓库地图|显示仓库地图|项目结构)"
    r")",
    re.IGNORECASE,
)
_IMPACT_RE = re.compile(
    r"(?:"
    r"\b(?P<i_en>impact of|what breaks|affected by|blast radius|stale wiki|"
    r"downstream impact)\b"
    r"|(?P<i_ru>влияние|что сломается)"
    r"|(?P<i_zh>影响|的影响)"
    r")",
    re.IGNORECASE,
)
_GLOBAL_RE = re.compile(
    r"(?:"
    r"\b(?P<s_en>synthesize|synthesis|across all projects|across projects|"
    r"overall architecture|compare across|global overview)\b"
    r"|(?P<s_ru>синтез|глобальный обзор)"
    r"|(?P<s_zh>跨项目|综合架构|全局概览)"
    r")",
    re.IGNORECASE,
)
_CROSS_LANG_RE = re.compile(
    r"(?:[\u0400-\u04FF].*[A-Za-z]|[A-Za-z].*[\u0400-\u04FF]|"
    r"[\u4e00-\u9fff].*[A-Za-z]|[A-Za-z].*[\u4e00-\u9fff])"
)


@dataclass(frozen=True)
class QueryAnalysis:
    query: str
    normalized_query: str
    intents: tuple[str, ...]
    exact_identifiers: tuple[str, ...]
    quoted_phrases: tuple[str, ...]
    recommended_profile: str


@dataclass(frozen=True)
class RetrievalTrace:
    requested_mode: str
    effective_mode: str
    signals_used: tuple[str, ...]
    fallback_reason: str | None
    corpus_generation: str
    partial: bool
    reranker_applied: bool = False
    reranker_model_id: str | None = None
    reranker_model_revision: str | None = None
    reranker_depth: int | None = None
    reranker_duration_ms: int | None = None
    reranker_fallback_reason: str | None = None
    # The signals the run asked for. A planned GRAPH run adds dense to what its
    # profile declares, so the profile alone cannot say a dense leg went missing
    # (audit 2026-09-26 C-10).
    signals_requested: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetrievalCandidate:
    candidate_id: str
    parent_id: str
    relative_path: str
    heading_path: tuple[str, ...]
    source_sha256: str
    byte_start: int
    byte_end: int
    bm25_rank: int | None
    bm25_score: float | None
    vector_rank: int | None
    vector_score: float | None
    graph_rank: int | None
    graph_score: float | None
    rrf_score: float
    rerank_score: float | None
    final_score: float
    evidence_ids: tuple[str, ...]
    # Typed provenance weighs on the score that decides the order; see
    # scripts/provenance.py. 1.0 means unknown or unweighted provenance.
    authority_weight: float = 1.0
    # What the page is — the second factor of the same weight. 1.0 means a type
    # this table does not rank, which includes code.
    type_weight: float = 1.0


@dataclass(frozen=True)
class RetrievalResult:
    candidates: tuple[RetrievalCandidate, ...]
    trace: RetrievalTrace
    analysis: QueryAnalysis
    display_meta: Mapping[str, Mapping[str, Any]] | None = None


def _profile_text(value: object) -> str:
    """The canonical spelling of a profile name, or a type error."""
    if not isinstance(value, str):
        raise TypeError("profile must be a string")
    return value.strip().upper().replace("-", "_")


def normalize_profile(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _profile_text(value)
    if normalized not in PROFILES:
        raise ValueError(f"unknown retrieval profile: {value}")
    return normalized


_INTENT_PATTERNS = (
    ("temporal", _TEMPORAL_RE),
    ("graph_relation", _GRAPH_RE),
    ("repo_map", _REPO_MAP_RE),
    ("impact", _IMPACT_RE),
    ("global_synthesis", _GLOBAL_RE),
)

# First match wins; the order is the priority the planner promises.
_PROFILE_BY_INTENT = (
    ("global_synthesis", "GLOBAL"),
    ("impact", "IMPACT"),
    ("repo_map", "REPO_MAP"),
    ("graph_relation", "GRAPH"),
    ("temporal", "TEMPORAL"),
    ("quoted_phrase", "EXACT"),
    ("exact_identifier", "EXACT"),
    ("question", "HYBRID"),
)


def _quoted_phrases(stripped: str) -> tuple[str, ...]:
    return tuple(
        match.group(1).strip()
        for match in _QUOTE_RE.finditer(stripped)
        if match.group(1).strip()
    )


def _slug_is_redundant(slug: str, identifiers: list[str]) -> bool:
    """A slug already covered by a path or a longer identifier adds nothing."""
    if "/" in slug or slug in identifiers:
        return True
    return any(slug in item for item in identifiers)


def _exact_identifiers(stripped: str) -> tuple[str, ...]:
    identifiers: list[str] = []
    for pattern, group in (
        (_PATH_RE, "path"),
        (_FILENAME_RE, "file"),
        (_CAMEL_RE, "camel"),
        (_SNAKE_RE, "snake"),
    ):
        identifiers.extend(match.group(group) for match in pattern.finditer(stripped))
    for match in _SLUG_RE.finditer(stripped):
        slug = match.group("slug")
        if not _slug_is_redundant(slug, identifiers):
            identifiers.append(slug)
    return tuple(dict.fromkeys(identifiers))


def _is_question(stripped: str, normalized: str) -> bool:
    if _QUESTION_RE.search(normalized):
        return True
    return stripped.endswith("?") or stripped.endswith("？")


def _shape_intents(
    stripped: str, normalized: str, phrases: tuple[str, ...], identifiers: tuple[str, ...]
) -> list[str]:
    """Intents that come from the query's shape rather than its wording."""
    present = (
        (bool(phrases), "quoted_phrase"),
        (bool(identifiers), "exact_identifier"),
        (_is_question(stripped, normalized), "question"),
    )
    return [intent for found, intent in present if found]


def _query_intents(
    stripped: str, normalized: str, phrases: tuple[str, ...], identifiers: tuple[str, ...]
) -> tuple[str, ...]:
    intents = _shape_intents(stripped, normalized, phrases, identifiers)
    intents.extend(
        name for name, pattern in _INTENT_PATTERNS if pattern.search(normalized)
    )
    if _CROSS_LANG_RE.search(stripped):
        intents.append("cross_language")
    return tuple(intents)


def _recommended_profile(intents: tuple[str, ...]) -> str:
    intent_set = set(intents)
    for intent, profile in _PROFILE_BY_INTENT:
        if intent in intent_set:
            return profile
    return "BASE"


def analyze_query(query: str) -> QueryAnalysis:
    """Deterministic query analysis used by the retrieval planner."""
    if not isinstance(query, str):
        raise TypeError("query must be a string")
    stripped = query.strip()
    normalized = " ".join(stripped.split())
    phrases = _quoted_phrases(stripped)
    identifiers = _exact_identifiers(stripped)
    intents = _query_intents(stripped, normalized, phrases, identifiers)
    return QueryAnalysis(
        query=stripped,
        normalized_query=normalized,
        intents=intents,
        exact_identifiers=identifiers,
        quoted_phrases=phrases,
        recommended_profile=_recommended_profile(intents),
    )


def as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def as_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def heading_path_of(value: object) -> tuple[str, ...]:
    if isinstance(value, (tuple, list)):
        return tuple(str(item) for item in value)
    if isinstance(value, str) and value:
        return (value,)
    return ()


def candidate_key(row: Mapping[str, Any]) -> str:
    for key in ("candidate_id", "chunk_id", "path", "relative_path"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    raise ValueError("retrieval hit is missing candidate identity")


def hit_path(row: Mapping[str, Any]) -> str:
    for key in ("relative_path", "path", "source_path"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return candidate_key(row)


# What a citation carries when the digest of its source is genuinely unknown.
# It satisfies the citation schema's shape, so it has to stay reachable, but it
# identifies nothing and is only ever used when the file cannot be read.
_UNKNOWN_DIGEST = "0" * 64




def _digest_of_page(relative_path: str) -> str | None:
    """The real digest of this page, or None when it cannot be read."""
    if not relative_path:
        return None
    path = runtime_roots.vault_root() / relative_path
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return None


def _is_real_digest(sha: object) -> bool:
    return isinstance(sha, str) and len(sha) == 64 and sha != _UNKNOWN_DIGEST


def source_sha256(row: Mapping[str, Any]) -> str:
    """The digest of the row's source.

    The legacy FTS index (retired 2026-09-23) stored no digest, so a row from it carried sixty-four
    zeros — a value that passes the citation schema while identifying nothing.
    The page is right there, so it is hashed instead, and the placeholder is
    left for the case where the file genuinely cannot be read.
    """
    sha = row.get("source_sha256") or row.get("sha256")
    if _is_real_digest(sha):
        return str(sha)
    return _digest_of_page(hit_path(row)) or _UNKNOWN_DIGEST


class RetrievalStopped(TimeoutError):
    """The caller's own deadline or cancel flag stopped the plan.

    Deliberately distinct from a `TimeoutError` a backend raises. A backend
    timeout says the work failed; this one says *we* stopped the work, and
    whatever earlier legs finished is still in hand. Only this class is
    salvageable, so a backend that times out keeps propagating exactly as it
    did before. It stays a `TimeoutError` so every existing caller and test
    that catches or matches one is unaffected, message included.
    """

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def check_deadline(deadline_monotonic: float | None) -> None:
    if deadline_monotonic is None:
        return

    if time.monotonic() >= float(deadline_monotonic):
        raise RetrievalStopped(
            "retrieval deadline exceeded", "deadline_expired_partial_result"
        )


def check_stopped(
    deadline_monotonic: float | None, cancelled: Callable[[], bool] | None
) -> None:
    check_deadline(deadline_monotonic)
    if cancelled is not None and cancelled():
        raise RetrievalStopped("retrieval cancelled", "cancelled_partial_result")


def note_generation_degradation(kind: str, error: BaseException) -> None:
    import generation_search

    generation_search.note_degradation(kind, error)


def first_present(row: Mapping[str, Any], keys: tuple[str, ...], fallback: Any) -> Any:
    """First usable value among `keys`, else the fallback (empty counts as absent)."""
    for key in keys:
        value = row.get(key)
        if value:
            return value
    return fallback
