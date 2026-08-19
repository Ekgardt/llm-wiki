"""Deterministic retrieval contract: query analysis, profiles, RRF fusion."""
from __future__ import annotations

import json
import queue
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from provenance import authority_weight

MAX_OPTIONAL_STRAGGLERS = 2
OPTIONAL_STAGE_MAX_SECONDS = 0.5
_OPTIONAL_STAGE_SLOTS = threading.BoundedSemaphore(MAX_OPTIONAL_STRAGGLERS)


def _normalized_filename_stem(value: str) -> str:
    name = Path(value.strip()).name.casefold()
    if name.endswith(".md"):
        name = name[:-3]
    return "-".join(part for part in re.split(r"[\s_-]+", name) if part)


def _first_exact_filename(
    candidates: Sequence[RetrievalCandidate], normalized: str
) -> RetrievalCandidate | None:
    for candidate in candidates:
        if _normalized_filename_stem(candidate.relative_path) == normalized:
            return candidate
    return None


def _promote_exact_filename(
    candidates: Sequence[RetrievalCandidate], query: str
) -> tuple[RetrievalCandidate, ...]:
    normalized = _normalized_filename_stem(query)
    if not normalized:
        return tuple(candidates)
    exact = _first_exact_filename(candidates, normalized)
    if exact is None:
        return tuple(candidates)
    return (exact, *(candidate for candidate in candidates if candidate is not exact))


class OptionalStageTimeout(TimeoutError):
    """An optional uninterruptible stage exceeded its isolated budget."""


def _require_optional_stage_time(
    deadline: float, cancelled: Callable[[], bool] | None
) -> None:
    if deadline - time.monotonic() <= 0:
        raise OptionalStageTimeout("optional stage deadline reached")
    if cancelled is not None and cancelled():
        raise OptionalStageTimeout("optional stage deadline reached")


def _run_optional_bounded(
    operation: Callable[[], Any],
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> Any:
    """Run optional work with a hard wait bound and capped daemon stragglers."""
    _require_optional_stage_time(deadline, cancelled)
    slots = _OPTIONAL_STAGE_SLOTS
    if not slots.acquire(blocking=False):
        raise OptionalStageTimeout("optional stage capacity exhausted")
    completed = threading.Event()
    result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
    _start_optional_worker(operation, result, completed, slots)
    _await_optional_stage(completed, deadline, cancelled)
    ok, value = result.get_nowait()
    if ok:
        return value
    raise value


def _start_optional_worker(
    operation: Callable[[], Any],
    result: queue.Queue[tuple[bool, Any]],
    completed: threading.Event,
    slots: threading.BoundedSemaphore,
) -> None:
    def run() -> None:
        try:
            result.put((True, operation()))
        except BaseException as exc:
            result.put((False, exc))
        finally:
            completed.set()
            slots.release()

    worker = threading.Thread(target=run, name="llm-wiki-optional-retrieval", daemon=True)
    try:
        worker.start()
    except BaseException:
        slots.release()
        raise


def _await_optional_stage(
    completed: threading.Event,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> None:
    """Wait out one optional stage; a straggler keeps running as a daemon."""
    stage_deadline = min(deadline, time.monotonic() + OPTIONAL_STAGE_MAX_SECONDS)
    while not completed.is_set():
        if cancelled is not None and cancelled():
            raise OptionalStageTimeout("optional stage cancelled")
        wait = stage_deadline - time.monotonic()
        if wait <= 0:
            raise OptionalStageTimeout("optional stage deadline reached")
        completed.wait(min(wait, 0.01))

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
BM25_WEIGHT = 2.0
DENSE_WEIGHT = 1.0
GRAPH_WEIGHT = 0.5

GRAPH_MAX_HOPS = 1
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
    "CO_CHANGED_WITH": 0.45,
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
    "IMPACT": (
        "CALLS",
        "CHECKPOINT_CHANGED_FILE",
        "CO_CHANGED_WITH",
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
_TEMPORAL_RE = re.compile(
    r"(?:"
    r"\b(?P<t_en>since|before|after|until|as of|yesterday|today|last week|"
    r"last month|last year|in \d{4}|from \d{4}|between \d{4})\b"
    r"|(?P<t_ru>с\s+\d{4}|после|до\s+\d{4}|вчера|сегодня|на этой неделе|"
    r"решения\s+с\s+\d{4})"
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


@dataclass(frozen=True)
class RetrievalResult:
    candidates: tuple[RetrievalCandidate, ...]
    trace: RetrievalTrace
    analysis: QueryAnalysis
    display_meta: Mapping[str, Mapping[str, Any]] | None = None


def _normalize_profile(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("profile must be a string")
    normalized = value.strip().upper().replace("-", "_")
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
    intents: list[str] = []
    if phrases:
        intents.append("quoted_phrase")
    if identifiers:
        intents.append("exact_identifier")
    if _is_question(stripped, normalized):
        intents.append("question")
    return intents


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


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _heading_path(value: object) -> tuple[str, ...]:
    if isinstance(value, (tuple, list)):
        return tuple(str(item) for item in value)
    if isinstance(value, str) and value:
        return (value,)
    return ()


def _candidate_key(row: Mapping[str, Any]) -> str:
    for key in ("candidate_id", "chunk_id", "path", "relative_path"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    raise ValueError("retrieval hit is missing candidate identity")


def _hit_path(row: Mapping[str, Any]) -> str:
    for key in ("relative_path", "path", "source_path"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return _candidate_key(row)


def _graph_seeds(
    lexical: Sequence[Mapping[str, Any]] | None,
    dense: Sequence[Mapping[str, Any]] | None,
) -> tuple[Mapping[str, Any], ...]:
    """Select rank-qualified seeds without comparing backend score magnitudes."""
    selected: dict[str, Mapping[str, Any]] = {}
    for rows in (lexical or (), dense or ()):
        for rank, row in enumerate(rows, start=1):
            if _seed_qualifies(row, rank):
                selected.setdefault(_candidate_key(row), row)
    return tuple(selected.values())[:GRAPH_SEED_LIMIT]


def _seed_qualifies(row: Mapping[str, Any], rank: int) -> bool:
    """A stated high confidence qualifies; otherwise the rank has to."""
    confidence = row.get("retrieval_confidence")
    if confidence is None:
        return rank <= GRAPH_SEED_LIMIT
    return str(confidence).casefold() == "high"


def _query_terms(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[\w-]+", text.casefold(), flags=re.UNICODE))


def _graph_text_relevance(query: str, row: Mapping[str, Any]) -> tuple[int, int]:
    terms = _query_terms(query)
    text = " ".join(
        str(row.get(field) or "")
        for field in ("title", "summary", "content", "relative_path", "path")
    )
    row_terms = _query_terms(text)
    return len(terms & row_terms), int(bool(terms) and terms.issubset(row_terms))


def _assertion_path(row: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    raw_path = row.get("assertion_path")
    if not isinstance(raw_path, (list, tuple)) or not raw_path:
        return ()
    path: list[dict[str, Any]] = []
    for raw_step in raw_path:
        step = _assertion_step(raw_step)
        if step is None:
            return ()
        path.append(step)
    return tuple(path)


def _assertion_step(raw_step: object) -> dict[str, Any] | None:
    """One path step, or None when its identity or evidence is unusable."""
    if not isinstance(raw_step, Mapping):
        return None
    assertion_id = raw_step.get("assertion_id")
    if not isinstance(assertion_id, str) or not assertion_id:
        return None
    evidence_ids = _evidence_id_tuple(raw_step.get("evidence_ids"))
    if not evidence_ids:
        return None
    step = dict(raw_step)
    step["evidence_ids"] = evidence_ids
    return step


def _evidence_id_tuple(raw_evidence: object) -> tuple[str, ...]:
    if not isinstance(raw_evidence, (list, tuple)):
        return ()
    return tuple(item for item in raw_evidence if isinstance(item, str) and item)


def _prepare_graph_hits(
    rows: Sequence[Mapping[str, Any]],
    *,
    query: str,
    requested_profile: str,
    seeds: Sequence[Mapping[str, Any]],
    directions: Sequence[str],
    edge_types: Sequence[str],
    per_seed_limit: int,
    global_limit: int,
) -> tuple[Mapping[str, Any], ...]:
    """Validate, text-rerank, decay, and cap graph expansions deterministically."""
    seed_ids = {_candidate_key(seed) for seed in seeds}
    allowed_directions = set(directions)
    allowed_edges = set(edge_types)
    direct_graph_query = requested_profile == "GRAPH"
    prepared: list[tuple[tuple[object, ...], dict[str, Any]]] = []
    for backend_rank, raw in enumerate(rows, start=1):
        row = dict(raw)
        seed_id = row.get("seed_id")
        if seed_id is None:
            # Independent pre-Task22 graph retrievers remain rank-only inputs.
            prepared.append(((0, 0, 0, backend_rank, _candidate_key(row)), row))
            continue
        if not isinstance(seed_id, str) or seed_id not in seed_ids:
            continue
        hop = _as_int(row.get("hop"), 0)
        direction = row.get("direction")
        edge_type = row.get("edge_type")
        path = _assertion_path(row)
        if (
            not 1 <= hop <= GRAPH_MAX_HOPS
            or direction not in allowed_directions
            or edge_type not in allowed_edges
            or not path
        ):
            continue
        evidence_ids = tuple(
            dict.fromkeys(
                evidence_id
                for step in path
                for evidence_id in step["evidence_ids"]
            )
        )
        overlap, full_match = _graph_text_relevance(query, row)
        if overlap == 0 and not direct_graph_query:
            continue
        decay = GRAPH_EDGE_DECAY[str(edge_type)] ** hop
        row.update(
            assertion_path=path,
            evidence_ids=evidence_ids,
            graph_boost=decay,
            graph_text_overlap=overlap,
        )
        prepared.append(
            (
                (
                    -full_match,
                    -overlap,
                    hop,
                    -decay,
                    seed_id,
                    _candidate_key(row),
                ),
                row,
            )
        )

    selected: dict[str, dict[str, Any]] = {}
    per_seed: dict[str, int] = {}
    for _key, row in sorted(prepared, key=lambda item: item[0]):
        seed_id = str(row.get("seed_id") or "")
        if seed_id and per_seed.get(seed_id, 0) >= per_seed_limit:
            continue
        candidate_id = _candidate_key(row)
        existing = selected.get(candidate_id)
        if existing is None:
            if len(selected) >= global_limit:
                continue
            selected[candidate_id] = dict(row)
        else:
            existing_path = list(existing.get("assertion_path") or ())
            for step in row.get("assertion_path") or ():
                if step not in existing_path:
                    existing_path.append(step)
            existing["assertion_path"] = tuple(existing_path)
            existing["evidence_ids"] = tuple(
                dict.fromkeys(
                    (*existing.get("evidence_ids", ()), *row.get("evidence_ids", ()))
                )
            )
            existing["graph_boost"] = max(
                float(existing.get("graph_boost") or 0.0),
                float(row.get("graph_boost") or 0.0),
            )
        if seed_id:
            per_seed[seed_id] = per_seed.get(seed_id, 0) + 1
    return tuple(selected.values())


def expand_evidence_graph(
    graph: Any,
    *,
    seeds: Sequence[Mapping[str, Any]],
    directions: Sequence[str],
    edge_types: Sequence[str],
    per_seed_limit: int = GRAPH_PER_SEED_LIMIT,
    global_limit: int = GRAPH_GLOBAL_LIMIT,
    deadline_monotonic: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[Mapping[str, Any], ...]:
    """Read one evidenced typed hop from a sealed ``EvidenceGraph`` handle."""
    if not 1 <= per_seed_limit <= 100 or not 1 <= global_limit <= 1000:
        raise ValueError("graph expansion caps are outside supported bounds")
    normalized_directions = tuple(dict.fromkeys(str(item) for item in directions))
    if not normalized_directions or any(item not in {"in", "out"} for item in normalized_directions):
        raise ValueError("graph directions must contain only in or out")
    normalized_edges = tuple(sorted(dict.fromkeys(str(item) for item in edge_types)))
    if not normalized_edges or len(normalized_edges) > 64:
        raise ValueError("graph edge types must contain between 1 and 64 values")

    results: list[Mapping[str, Any]] = []
    edge_placeholders = ",".join("?" for _ in normalized_edges)
    for seed in seeds:
        _check_stopped(deadline_monotonic, cancelled)
        seed_id = _candidate_key(seed)
        seed_path = _hit_path(seed)
        seed_results: list[Mapping[str, Any]] = []
        for direction in normalized_directions:
            _check_stopped(deadline_monotonic, cancelled)
            source_column, target_column = (
                ("source_node_id", "target_node_id")
                if direction == "out"
                else ("target_node_id", "source_node_id")
            )
            rows = graph._execute(
                f"""
                SELECT a.assertion_id, a.source_node_id, a.target_node_id,
                       a.edge_type, a.confidence, a.authority, a.extractor,
                       neighbor.node_id, neighbor.kind, neighbor.metadata_json,
                       target_occ.byte_start, target_occ.byte_end,
                       target_source.relative_path, target_source.sha256,
                       target_source.content,
                       e.evidence_id, e.source_id AS evidence_source_id,
                       e.byte_start AS evidence_byte_start,
                       e.byte_end AS evidence_byte_end, e.span_sha256,
                       evidence_source.relative_path AS evidence_relative_path
                FROM occurrence seed_occ
                JOIN source seed_source ON seed_source.source_id = seed_occ.source_id
                JOIN assertion a ON a.{source_column} = seed_occ.node_id
                JOIN node neighbor ON neighbor.node_id = a.{target_column}
                JOIN occurrence target_occ ON target_occ.node_id = neighbor.node_id
                JOIN source target_source ON target_source.source_id = target_occ.source_id
                JOIN evidence e ON e.assertion_id = a.assertion_id
                JOIN source evidence_source ON evidence_source.source_id = e.source_id
                WHERE seed_source.relative_path = ?
                  AND a.resolution = 'resolved'
                  AND a.target_node_id IS NOT NULL
                  AND a.edge_type IN ({edge_placeholders})
                  AND target_occ.occurrence_id = (
                    SELECT min(chosen.occurrence_id) FROM occurrence chosen
                    WHERE chosen.node_id = neighbor.node_id
                  )
                ORDER BY a.edge_type, neighbor.node_id, a.assertion_id, e.evidence_id
                LIMIT ?
                """,
                (seed_path, *normalized_edges),
                max_rows=min(1000, max(32, per_seed_limit * 16)),
                deadline=deadline_monotonic,
            )
            grouped: dict[tuple[str, str], dict[str, Any]] = {}
            for row in rows:
                _check_stopped(deadline_monotonic, cancelled)
                key = (str(row["assertion_id"]), str(row["node_id"]))
                item = grouped.get(key)
                evidence = {
                    "evidence_id": str(row["evidence_id"]),
                    "source_id": str(row["evidence_source_id"]),
                    "relative_path": str(row["evidence_relative_path"]),
                    "byte_start": int(row["evidence_byte_start"]),
                    "byte_end": int(row["evidence_byte_end"]),
                    "span_sha256": str(row["span_sha256"]),
                }
                if item is None:
                    metadata = json.loads(str(row["metadata_json"]))
                    content = row["content"]
                    if isinstance(content, bytes):
                        content = content.decode("utf-8", errors="replace")
                    item = {
                        "candidate_id": str(row["node_id"]),
                        "parent_id": str(row["relative_path"]),
                        "relative_path": str(row["relative_path"]),
                        "source_sha256": str(row["sha256"]),
                        "byte_start": int(row["byte_start"]),
                        "byte_end": int(row["byte_end"]),
                        "title": metadata.get("name") or Path(str(row["relative_path"])).stem,
                        "content": str(content),
                        "seed_id": seed_id,
                        "hop": 1,
                        "direction": direction,
                        "edge_type": str(row["edge_type"]),
                        "assertion_path": [
                            {
                                "assertion_id": str(row["assertion_id"]),
                                "source_node_id": str(row["source_node_id"]),
                                "target_node_id": str(row["target_node_id"]),
                                "edge_type": str(row["edge_type"]),
                                "direction": direction,
                                "confidence": str(row["confidence"]),
                                "authority": str(row["authority"]),
                                "extractor": str(row["extractor"]),
                                "evidence_ids": [],
                                "evidence": [],
                            }
                        ],
                        "evidence_ids": [],
                        "generation": getattr(graph, "generation_id", None),
                    }
                    grouped[key] = item
                step = item["assertion_path"][0]
                step["evidence_ids"].append(evidence["evidence_id"])
                step["evidence"].append(evidence)
                item["evidence_ids"].append(evidence["evidence_id"])
            for item in grouped.values():
                _check_stopped(deadline_monotonic, cancelled)
                step = item["assertion_path"][0]
                step["evidence_ids"] = tuple(step["evidence_ids"])
                step["evidence"] = tuple(step["evidence"])
                item["assertion_path"] = tuple(item["assertion_path"])
                item["evidence_ids"] = tuple(item["evidence_ids"])
                seed_results.append(item)
        seed_results.sort(
            key=lambda item: (
                str(item["edge_type"]),
                str(item["candidate_id"]),
                str(item["direction"]),
                str(item["assertion_path"][0]["assertion_id"]),
            )
        )
        results.extend(seed_results[:per_seed_limit])
        if len(results) >= global_limit:
            break
    return tuple(results[:global_limit])


def _weigh_by_authority(
    scores: Mapping[str, float],
    meta: dict[str, dict[str, Any]],
) -> dict[str, float]:
    """Multiply each fused score by its typed-provenance weight.

    The weight is recorded on the candidate so the ordering can be explained.
    """
    weighted: dict[str, float] = {}
    for key, value in scores.items():
        weight = authority_weight(meta[key].get("authority"))
        meta[key]["authority_weight"] = weight
        weighted[key] = value * weight
    return weighted


def fuse_rrf(
    *,
    lexical: Sequence[Mapping[str, Any]] | None,
    dense: Sequence[Mapping[str, Any]] | None,
    graph: Sequence[Mapping[str, Any]] | None,
    k: int = RRF_K,
) -> tuple[tuple[RetrievalCandidate, ...], dict[str, dict[str, Any]]]:
    """Fuse independent ranked lists with weighted rank-only RRF.

    Larger final_score wins. Raw backend magnitudes are preserved on the
    candidate but never enter the fusion formula. Equal RRF ties break by
    candidate_id ascending.
    """
    scores: dict[str, float] = {}
    meta: dict[str, dict[str, Any]] = {}

    def ensure(row: Mapping[str, Any]) -> str:
        key = _candidate_key(row)
        if key not in meta:
            path = _hit_path(row)
            sha = row.get("source_sha256") or row.get("sha256") or ("0" * 64)
            if not isinstance(sha, str) or len(sha) != 64:
                sha = "0" * 64
            meta[key] = {
                "candidate_id": key,
                "parent_id": str(row.get("parent_id") or row.get("parent_page") or path),
                "relative_path": path,
                "heading_path": _heading_path(
                    row.get("heading_path") or row.get("heading_ancestry")
                ),
                "source_sha256": sha,
                "byte_start": _as_int(row.get("byte_start"), 0),
                "byte_end": _as_int(row.get("byte_end"), 0),
                "bm25_rank": None,
                "bm25_score": None,
                "vector_rank": None,
                "vector_score": None,
                "graph_rank": None,
                "graph_score": None,
                "evidence_ids": tuple(
                    str(item)
                    for item in (row.get("evidence_ids") or ())
                    if isinstance(item, str) and item
                ),
                "title": row.get("title"),
                "summary": row.get("summary"),
                "content": row.get("content") or row.get("summary"),
                "project": row.get("project"),
                "timestamp": row.get("timestamp"),
                "chunk_id": row.get("chunk_id") or key,
                "authority": row.get("authority"),
                "confidence": row.get("confidence"),
                "status": row.get("status"),
                "type": row.get("type"),
                "valid_from": row.get("valid_from"),
                "valid_to": row.get("valid_to"),
                "language": row.get("language"),
                "source_id": row.get("source_id"),
                "lance_distance": row.get("lance_distance"),
                "graph_seed_id": row.get("seed_id"),
                "graph_direction": row.get("direction"),
                "graph_edge_type": row.get("edge_type"),
                "assertion_path": _assertion_path(row),
                "graph_text_overlap": row.get("graph_text_overlap"),
            }
        else:
            graph_fields = {
                "graph_seed_id": row.get("seed_id"),
                "graph_direction": row.get("direction"),
                "graph_edge_type": row.get("edge_type"),
                "assertion_path": _assertion_path(row),
                "graph_text_overlap": row.get("graph_text_overlap"),
            }
            for field, value in graph_fields.items():
                if meta[key].get(field) in (None, "", ()) and value not in (None, "", ()):
                    meta[key][field] = value
            incoming_evidence = tuple(
                str(item)
                for item in (row.get("evidence_ids") or ())
                if isinstance(item, str) and item
            )
            if incoming_evidence:
                meta[key]["evidence_ids"] = tuple(
                    dict.fromkeys((*meta[key]["evidence_ids"], *incoming_evidence))
                )
            # Prefer first non-empty display fields from any backend.
            for field in (
                "title",
                "summary",
                "content",
                "project",
                "timestamp",
                "chunk_id",
                "authority",
                "confidence",
                "status",
                "type",
                "valid_from",
                "valid_to",
                "language",
                "source_id",
                "lance_distance",
                "graph_seed_id",
                "graph_direction",
                "graph_edge_type",
                "assertion_path",
                "graph_text_overlap",
            ):
                if meta[key].get(field) in (None, "") and row.get(field) not in (None, ""):
                    meta[key][field] = row.get(field)
        return key

    if lexical:
        for rank, row in enumerate(lexical, start=1):
            key = ensure(row)
            scores[key] = scores.get(key, 0.0) + BM25_WEIGHT / (k + rank)
            meta[key]["bm25_rank"] = rank
            meta[key]["bm25_score"] = _as_float(
                row.get("bm25_score") if "bm25_score" in row else row.get("score")
            )

    if dense:
        for rank, row in enumerate(dense, start=1):
            key = ensure(row)
            scores[key] = scores.get(key, 0.0) + DENSE_WEIGHT / (k + rank)
            meta[key]["vector_rank"] = rank
            meta[key]["vector_score"] = _as_float(
                row.get("vector_score") if "vector_score" in row else row.get("score")
            )

    if graph:
        for rank, row in enumerate(graph, start=1):
            key = ensure(row)
            # Rank-only contribution; store raw boost separately.
            scores[key] = scores.get(key, 0.0) + GRAPH_WEIGHT / (k + rank)
            meta[key]["graph_rank"] = rank
            meta[key]["graph_score"] = _as_float(
                row.get("graph_boost") if "graph_boost" in row else row.get("score")
            )

    weighted = _weigh_by_authority(scores, meta)
    ordered = sorted(weighted, key=lambda item: (-weighted[item], item))
    candidates: list[RetrievalCandidate] = []
    for key in ordered:
        rrf = round(scores[key], 6)
        info = meta[key]
        candidates.append(
            RetrievalCandidate(
                candidate_id=info["candidate_id"],
                parent_id=info["parent_id"],
                relative_path=info["relative_path"],
                heading_path=info["heading_path"],
                source_sha256=info["source_sha256"],
                byte_start=info["byte_start"],
                byte_end=info["byte_end"],
                bm25_rank=info["bm25_rank"],
                bm25_score=info["bm25_score"],
                vector_rank=info["vector_rank"],
                vector_score=info["vector_score"],
                graph_rank=info["graph_rank"],
                graph_score=info["graph_score"],
                rrf_score=rrf,
                rerank_score=None,
                final_score=round(weighted[key], 6),
                evidence_ids=info["evidence_ids"],
                authority_weight=info["authority_weight"],
            )
        )
    return tuple(candidates), meta


def _dense_is_missing(ran_dense: bool, dense_available: bool | None) -> bool:
    if dense_available is False:
        return True
    return dense_available is None and not ran_dense


def _dense_signal(
    wanted: Sequence[str], ran_dense: bool, dense_available: bool | None
) -> tuple[str | None, str | None]:
    """(signal, fallback reason) for the dense backend."""
    if "dense" not in wanted:
        return None, None
    if dense_available is True and ran_dense:
        return "dense", None
    if _dense_is_missing(ran_dense, dense_available):
        return None, "dense_unavailable"
    return None, None


def _graph_signal(
    wanted: Sequence[str],
    ran_graph: bool,
    graph_available: bool | None,
    graph_enabled: bool,
) -> tuple[str | None, str | None]:
    """(signal, fallback reason) for the graph backend."""
    if "graph" not in wanted:
        return None, None
    if not graph_enabled:
        return None, "graph_disabled"
    if graph_available is True and ran_graph:
        return "graph", None
    return None, "graph_unavailable"


def _effective_for_hybrid(signals: set[str], requested: str) -> str:
    if "dense" in signals:
        return "HYBRID"
    return _lexical_or_requested(signals, requested)


def _effective_for_graph(signals: set[str], requested: str) -> str:
    if "graph" in signals:
        return "GRAPH"
    return _lexical_or_requested(signals, requested)


def _effective_for_global(signals: set[str], requested: str) -> str:
    if "dense" in signals and "graph" in signals:
        return "GLOBAL"
    if "dense" in signals:
        return "HYBRID"
    return _effective_for_graph(signals, requested)


def _effective_for_graph_profile(signals: set[str], requested: str) -> str:
    """REPO_MAP and IMPACT keep their name only while the graph answered."""
    if "graph" in signals:
        return requested
    return _lexical_or_requested(signals, requested)


def _lexical_or_requested(signals: set[str], requested: str) -> str:
    if "lexical" in signals:
        return "BASE"
    return requested


def _effective_mode(requested: str, signals: set[str]) -> str:
    resolvers = {
        "HYBRID": _effective_for_hybrid,
        "GRAPH": _effective_for_graph,
        "GLOBAL": _effective_for_global,
        "REPO_MAP": _effective_for_graph_profile,
        "IMPACT": _effective_for_graph_profile,
    }
    resolver = resolvers.get(requested)
    if resolver is not None:
        return resolver(signals, requested)
    if signals:
        return requested
    return "BASE"


def _resolve_effective_mode(
    requested: str,
    *,
    wanted: Sequence[str],
    ran_lexical: bool,
    ran_dense: bool,
    ran_graph: bool,
    dense_available: bool | None,
    graph_available: bool | None,
    graph_enabled: bool,
) -> tuple[str, str | None, tuple[str, ...]]:
    """Compute truthful effective mode, signals, and a single fallback reason."""
    dense_signal, fallback = _dense_signal(wanted, ran_dense, dense_available)
    graph_signal, graph_fallback = _graph_signal(
        wanted, ran_graph, graph_available, graph_enabled
    )
    signals = _collected_signals(
        _lexical_signal(wanted, ran_lexical), dense_signal, graph_signal, ran_lexical
    )
    return (
        _effective_mode(requested, set(signals)),
        fallback or graph_fallback,
        tuple(signals),
    )


def _collected_signals(
    lexical: list[str], dense: str | None, graph: str | None, ran_lexical: bool
) -> list[str]:
    """Signals that actually answered; a lexical-only run still says so."""
    signals = [*lexical, *(item for item in (dense, graph) if item is not None)]
    if not signals and ran_lexical:
        return ["lexical"]
    return signals


def _lexical_signal(wanted: Sequence[str], ran_lexical: bool) -> list[str]:
    if "lexical" in wanted and ran_lexical:
        return ["lexical"]
    return []


def _check_deadline(deadline_monotonic: float | None) -> None:
    if deadline_monotonic is None:
        return
    import time

    if time.monotonic() >= float(deadline_monotonic):
        raise TimeoutError("retrieval deadline exceeded")


def _check_stopped(
    deadline_monotonic: float | None, cancelled: Callable[[], bool] | None
) -> None:
    _check_deadline(deadline_monotonic)
    if cancelled is not None and cancelled():
        raise TimeoutError("retrieval cancelled")


def retrieve(
    query: str,
    *,
    requested_profile: str | None = None,
    scope: str = "all",
    limit: int = 10,
    project: str | None = None,
    since: str | None = None,
    as_of: str | None = None,
    lexical_backend: BackendFn | None = None,
    dense_backend: BackendFn | None = None,
    graph_backend: BackendFn | None = None,
    corpus_generation: str = "legacy",
    graph_enabled: bool = True,
    rerank_enabled: bool = True,
    partial: bool = False,
    deadline_monotonic: float | None = None,
    max_candidates: int | None = None,
    cancelled: Callable[[], bool] | None = None,
    graph_per_seed_limit: int = GRAPH_PER_SEED_LIMIT,
    graph_global_limit: int = GRAPH_GLOBAL_LIMIT,
    graph_edge_families: Mapping[str, bool] | None = None,
) -> RetrievalResult:
    """Plan and execute retrieval with truthful mode/signal reporting.

    Lexical and dense backends are invoked independently with identical hard
    filters. Fusion is rank-only RRF. Raw backend scores stay on candidates.
    """
    _check_stopped(deadline_monotonic, cancelled)
    analysis = analyze_query(query)
    requested = _normalize_profile(requested_profile) or analysis.recommended_profile
    if max_candidates is not None and int(max_candidates) > 0:
        backend_limit = min(limit if limit > 0 else int(max_candidates), int(max_candidates))
    else:
        backend_limit = limit
    filters = {
        "query": analysis.normalized_query or analysis.query,
        "scope": scope,
        "limit": backend_limit,
        "project": project,
        "since": since,
        "as_of": as_of,
    }

    wanted = PROFILE_SIGNALS[requested]
    if (
        isinstance(graph_per_seed_limit, bool)
        or not isinstance(graph_per_seed_limit, int)
        or not 1 <= graph_per_seed_limit <= 100
    ):
        raise ValueError("graph_per_seed_limit must be between 1 and 100")
    if (
        isinstance(graph_global_limit, bool)
        or not isinstance(graph_global_limit, int)
        or not 1 <= graph_global_limit <= 1000
    ):
        raise ValueError("graph_global_limit must be between 1 and 1000")
    if graph_edge_families is not None:
        if not isinstance(graph_edge_families, Mapping) or any(
            edge not in GRAPH_EDGE_DECAY or not isinstance(enabled, bool)
            for edge, enabled in graph_edge_families.items()
        ):
            raise ValueError("graph_edge_families must map known edge types to booleans")
    lexical_hits: Sequence[Mapping[str, Any]] | None = None
    dense_hits: Sequence[Mapping[str, Any]] | None = None
    graph_hits: Sequence[Mapping[str, Any]] | None = None

    ran_lexical = False
    ran_dense = False
    ran_graph = False
    dense_available: bool | None = None
    graph_available: bool | None = None
    graph_failure: str | None = None
    optional_failure: str | None = None

    if lexical_backend is not None and "lexical" in wanted:
        _check_stopped(deadline_monotonic, cancelled)
        lexical_hits = lexical_backend(**filters) or ()
        ran_lexical = True
        _check_stopped(deadline_monotonic, cancelled)

    if dense_backend is not None and "dense" in wanted:
        _check_stopped(deadline_monotonic, cancelled)
        try:
            dense_hits = (
                _run_optional_bounded(
                    lambda: dense_backend(**filters),
                    deadline=deadline_monotonic,
                    cancelled=cancelled,
                )
                if deadline_monotonic is not None
                else dense_backend(**filters)
            )
            ran_dense = True
            # None ⇒ backend unavailable; empty sequence ⇒ available but no hits.
            dense_available = dense_hits is not None
        except OptionalStageTimeout:
            dense_hits = None
            dense_available = False
            optional_failure = "optional_stage_timeout"
            partial = True
        _check_stopped(deadline_monotonic, cancelled)

    if graph_backend is not None and "graph" in wanted and graph_enabled:
        _check_stopped(deadline_monotonic, cancelled)
        directions = GRAPH_PROFILE_DIRECTIONS.get(requested, ("out",))
        edge_types = GRAPH_PROFILE_EDGE_TYPES.get(requested, tuple(GRAPH_EDGE_DECAY))
        if graph_edge_families:
            edge_types = tuple(
                edge_type
                for edge_type in edge_types
                if graph_edge_families.get(edge_type, True)
            )
        seeds = _graph_seeds(lexical_hits, dense_hits)
        try:
            raw_graph_hits = graph_backend(
                **filters,
                seeds=seeds,
                max_hops=GRAPH_MAX_HOPS,
                directions=directions,
                edge_types=edge_types,
                edge_decay={edge: GRAPH_EDGE_DECAY[edge] for edge in edge_types},
                per_seed_limit=graph_per_seed_limit,
                global_limit=graph_global_limit,
                deadline_monotonic=deadline_monotonic,
                corpus_generation=corpus_generation,
            )
            graph_hits = (
                None
                if raw_graph_hits is None
                else _prepare_graph_hits(
                    raw_graph_hits,
                    query=analysis.normalized_query or analysis.query,
                    requested_profile=requested,
                    seeds=seeds,
                    directions=directions,
                    edge_types=edge_types,
                    per_seed_limit=graph_per_seed_limit,
                    global_limit=graph_global_limit,
                )
            )
            ran_graph = True
            graph_available = graph_hits is not None
        except TimeoutError:
            raise
        except GenerationSealChanged:
            raise
        except Exception:
            graph_hits = None
            graph_available = False
            graph_failure = "graph_error"
        _check_stopped(deadline_monotonic, cancelled)
    elif "graph" in wanted and not graph_enabled:
        graph_available = False

    effective, fallback, signals = _resolve_effective_mode(
        requested,
        wanted=wanted,
        ran_lexical=ran_lexical,
        ran_dense=ran_dense,
        ran_graph=ran_graph,
        dense_available=dense_available,
        graph_available=graph_available,
        graph_enabled=graph_enabled,
    )
    fallback = graph_failure or fallback

    fuse_lexical = lexical_hits if "lexical" in signals else None
    fuse_dense = dense_hits if "dense" in signals and dense_hits is not None else None
    fuse_graph = graph_hits if "graph" in signals and graph_hits is not None else None
    candidates, display_meta = fuse_rrf(
        lexical=fuse_lexical, dense=fuse_dense, graph=fuse_graph
    )
    candidates = _promote_exact_filename(
        candidates, analysis.normalized_query or analysis.query
    )
    _check_stopped(deadline_monotonic, cancelled)
    if max_candidates is not None and int(max_candidates) > 0:
        candidates = candidates[: int(max_candidates)]

    # Conditional reranking (Task 13).
    signal_list = list(signals)
    reranker_applied = False
    reranker_model_id: str | None = None
    reranker_model_revision: str | None = None
    reranker_depth: int | None = None
    reranker_duration_ms: int | None = None
    reranker_fallback_reason: str | None = None
    if candidates and rerank_enabled:
        _check_stopped(deadline_monotonic, cancelled)
        try:
            from reranker import rerank as _rerank
            from reranker import should_rerank

            legacy_rows = []
            query_norm = (analysis.normalized_query or analysis.query).casefold().strip()
            exact_title_hit = False
            for c in candidates:
                info = display_meta.get(c.candidate_id, {})
                title = str(info.get("title") or Path(c.relative_path).stem)
                summary = str(info.get("summary") or "")
                content = str(info.get("content") or summary or "")
                if title.casefold().strip() == query_norm:
                    exact_title_hit = True
                legacy_rows.append(
                    {
                        "candidate_id": c.candidate_id,
                        "path": c.relative_path,
                        "relative_path": c.relative_path,
                        "summary": summary,
                        "content": content,
                        "title": title,
                        "rrf_score": c.rrf_score,
                        "score": c.rrf_score,
                        "bm25_rank": c.bm25_rank,
                        "vector_rank": c.vector_rank,
                        "bm25_score": c.bm25_score,
                        "vector_score": c.vector_score,
                        "graph_rank": c.graph_rank,
                        "graph_score": c.graph_score,
                        "source_sha256": c.source_sha256,
                        "heading_path": c.heading_path,
                        "parent_id": c.parent_id,
                        "byte_start": c.byte_start,
                        "byte_end": c.byte_end,
                        "evidence_ids": c.evidence_ids,
                        "lance_distance": info.get("lance_distance"),
                        "authority": info.get("authority"),
                        "authority_weight": c.authority_weight,
                    }
                )
            if exact_title_hit:
                apply, skip_reason = False, "exact_title_bypass"
                # Promote exact title match to rank 1 before any rerank.
                legacy_rows.sort(
                    key=lambda row: (
                        0 if str(row.get("title", "")).casefold().strip() == query_norm else 1,
                        -float(row.get("rrf_score") or 0.0),
                        str(row.get("candidate_id") or ""),
                    )
                )
                rebuilt_exact: list[RetrievalCandidate] = []
                id_map = {c.candidate_id: c for c in candidates}
                for row in legacy_rows:
                    base = id_map.get(str(row["candidate_id"]))
                    if base is None:
                        continue
                    rebuilt_exact.append(base)
                if rebuilt_exact:
                    candidates = tuple(rebuilt_exact)
            else:
                apply, skip_reason = should_rerank(
                    profile=requested,
                    candidates=legacy_rows,
                    analysis_intents=analysis.intents,
                    rerank_enabled=rerank_enabled,
                )
            if not apply:
                reranker_fallback_reason = skip_reason
            else:
                pool_limit = max(limit, 20) if limit > 0 else 20
                if max_candidates is not None and int(max_candidates) > 0:
                    pool_limit = min(pool_limit, int(max_candidates))
                def rerank_call():
                    return _rerank(
                        analysis.normalized_query or analysis.query,
                        legacy_rows[:pool_limit],
                        limit=pool_limit,
                        text_field="content",
                    )
                reranked = (
                    _run_optional_bounded(
                        rerank_call,
                        deadline=deadline_monotonic,
                        cancelled=cancelled,
                    )
                    if deadline_monotonic is not None
                    else rerank_call()
                )
                _check_stopped(deadline_monotonic, cancelled)
                if reranked and reranked[0].get("reranker_applied"):
                    signal_list.append("reranker")
                    reranker_applied = True
                    reranker_model_id = reranked[0].get("reranker_model_id")
                    reranker_model_revision = reranked[0].get("reranker_model_revision")
                    reranker_depth = reranked[0].get("reranker_depth")
                    reranker_duration_ms = reranked[0].get("reranker_duration_ms")
                    reranker_fallback_reason = None
                    rebuilt: list[RetrievalCandidate] = []
                    for row in reranked:
                        rebuilt.append(
                            RetrievalCandidate(
                                candidate_id=str(row.get("candidate_id") or row.get("path")),
                                parent_id=str(row.get("parent_id") or row.get("path") or ""),
                                relative_path=str(
                                    row.get("relative_path") or row.get("path") or ""
                                ),
                                heading_path=_heading_path(row.get("heading_path")),
                                source_sha256=str(row.get("source_sha256") or ("0" * 64)),
                                byte_start=_as_int(row.get("byte_start"), 0),
                                byte_end=_as_int(row.get("byte_end"), 0),
                                bm25_rank=row.get("bm25_rank")
                                if isinstance(row.get("bm25_rank"), int)
                                else None,
                                bm25_score=_as_float(row.get("bm25_score")),
                                vector_rank=row.get("vector_rank")
                                if isinstance(row.get("vector_rank"), int)
                                else None,
                                vector_score=_as_float(row.get("vector_score")),
                                graph_rank=row.get("graph_rank")
                                if isinstance(row.get("graph_rank"), int)
                                else None,
                                graph_score=_as_float(row.get("graph_score")),
                                rrf_score=float(row.get("rrf_score") or 0.0),
                                rerank_score=_as_float(row.get("rerank_score")),
                                authority_weight=float(
                                    row.get("authority_weight") or 1.0
                                ),
                                final_score=float(
                                    row.get("final_score")
                                    or row.get("rrf_score")
                                    or 0.0
                                ),
                                evidence_ids=tuple(
                                    str(x)
                                    for x in (row.get("evidence_ids") or ())
                                    if isinstance(x, str)
                                ),
                            )
                        )
                    candidates = tuple(rebuilt)
                elif reranked:
                    reranker_fallback_reason = str(
                        reranked[0].get("reranker_fallback_reason")
                        or "reranker_unavailable"
                    )
                    reranker_model_id = reranked[0].get("reranker_model_id")
                    reranker_model_revision = reranked[0].get("reranker_model_revision")
                    reranker_depth = reranked[0].get("reranker_depth")
                    reranker_duration_ms = reranked[0].get("reranker_duration_ms")
        except OptionalStageTimeout:
            reranker_fallback_reason = "optional_stage_timeout"
            optional_failure = optional_failure or "optional_stage_timeout"
            partial = True
        except TimeoutError:
            raise
        except Exception:
            reranker_fallback_reason = "reranker_error"

    candidates = _promote_exact_filename(
        candidates, analysis.normalized_query or analysis.query
    )
    _check_stopped(deadline_monotonic, cancelled)
    if limit > 0:
        candidates = candidates[:limit]

    trace = RetrievalTrace(
        requested_mode=requested,
        effective_mode=effective,
        signals_used=tuple(dict.fromkeys(signal_list)),
        fallback_reason=optional_failure or fallback,
        corpus_generation=corpus_generation,
        partial=partial,
        reranker_applied=reranker_applied,
        reranker_model_id=str(reranker_model_id) if reranker_model_id else None,
        reranker_model_revision=(
            str(reranker_model_revision) if reranker_model_revision else None
        ),
        reranker_depth=int(reranker_depth) if isinstance(reranker_depth, int) else None,
        reranker_duration_ms=(
            int(reranker_duration_ms) if isinstance(reranker_duration_ms, int) else None
        ),
        reranker_fallback_reason=reranker_fallback_reason,
    )
    return RetrievalResult(
        candidates=candidates,
        trace=trace,
        analysis=analysis,
        display_meta=display_meta,
    )


def trace_to_dict(trace: RetrievalTrace) -> dict[str, object]:
    return {
        "schema_version": "retrieval-trace/v1",
        "requested_mode": trace.requested_mode,
        "effective_mode": trace.effective_mode,
        "signals_used": list(trace.signals_used),
        "fallback_reason": trace.fallback_reason,
        "corpus_generation": trace.corpus_generation,
        "partial": trace.partial,
        "reranker_applied": trace.reranker_applied,
        "reranker_model_id": trace.reranker_model_id,
        "reranker_model_revision": trace.reranker_model_revision,
        "reranker_depth": trace.reranker_depth,
        "reranker_duration_ms": trace.reranker_duration_ms,
        "reranker_fallback_reason": trace.reranker_fallback_reason,
    }


def candidates_to_legacy(
    result: RetrievalResult,
    *,
    titles: Mapping[str, str] | None = None,
    summaries: Mapping[str, str] | None = None,
    projects: Mapping[str, str] | None = None,
    timestamps: Mapping[str, str] | None = None,
    display_meta: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Convert orchestrator output to the historical search() dict rows."""
    title_map = titles or {}
    summary_map = summaries or {}
    project_map = projects or {}
    timestamp_map = timestamps or {}
    meta = display_meta if display_meta is not None else (result.display_meta or {})
    rows: list[dict[str, Any]] = []
    for candidate in result.candidates:
        path = candidate.relative_path
        info = meta.get(candidate.candidate_id, {}) if isinstance(meta, dict) else {}
        title = title_map.get(path) or info.get("title") or Path(path).stem
        summary = summary_map.get(path) or info.get("summary") or ""
        project = project_map.get(path) or info.get("project") or ""
        timestamp = timestamp_map.get(path) or info.get("timestamp") or ""
        row: dict[str, Any] = {
            "path": path,
            "title": title,
            "summary": summary,
            "score": round(candidate.final_score, 4),
            "project": project,
            "timestamp": timestamp,
            "candidate_id": candidate.candidate_id,
            "chunk_id": info.get("chunk_id") or candidate.candidate_id,
            "source_sha256": candidate.source_sha256,
            "heading_ancestry": list(candidate.heading_path),
            "bm25_rank": candidate.bm25_rank,
            "bm25_score": candidate.bm25_score,
            "vector_rank": candidate.vector_rank,
            "vector_score": candidate.vector_score,
            "graph_rank": candidate.graph_rank,
            "graph_score": candidate.graph_score,
            "rrf_score": round(candidate.rrf_score, 4),
            "rerank_score": candidate.rerank_score,
            "final_score": round(candidate.final_score, 4),
            "requested_mode": result.trace.requested_mode,
            "effective_mode": result.trace.effective_mode,
            "signals_used": list(result.trace.signals_used),
            "fallback_reason": result.trace.fallback_reason,
            "generation": result.trace.corpus_generation,
            "partial": result.trace.partial,
            "reranker_applied": result.trace.reranker_applied,
            "reranker_model_id": result.trace.reranker_model_id,
            "reranker_model_revision": result.trace.reranker_model_revision,
            "reranker_depth": result.trace.reranker_depth,
            "reranker_duration_ms": result.trace.reranker_duration_ms,
            "reranker_fallback_reason": result.trace.reranker_fallback_reason,
        }
        for key in (
            "authority",
            "confidence",
            "status",
            "type",
            "valid_from",
            "valid_to",
            "language",
            "source_id",
            "content",
            "lance_distance",
            "graph_seed_id",
            "graph_direction",
            "graph_edge_type",
            "graph_text_overlap",
        ):
            if info.get(key) not in (None, ""):
                row[key] = info[key]
        assertion_path = info.get("assertion_path")
        if assertion_path:
            row["assertion_path"] = [
                {
                    **dict(step),
                    "evidence_ids": list(step.get("evidence_ids") or ()),
                }
                for step in assertion_path
            ]
        if candidate.evidence_ids:
            row["evidence_ids"] = list(candidate.evidence_ids)
        rows.append(row)
    return rows


def _backend_hit_from_legacy(row: Mapping[str, Any], *, score_key: str = "score") -> dict[str, Any]:
    path = str(row.get("path") or row.get("relative_path") or "")
    candidate_id = str(
        row.get("candidate_id")
        or row.get("chunk_id")
        or row.get("slug")
        or Path(path).stem
        or path
    )
    hit: dict[str, Any] = {
        "candidate_id": candidate_id,
        "chunk_id": row.get("chunk_id") or candidate_id,
        "parent_id": str(row.get("parent_id") or row.get("parent_page") or path),
        "relative_path": path,
        "path": path,
        "heading_path": row.get("heading_path") or row.get("heading_ancestry") or (),
        "source_sha256": row.get("source_sha256") or ("0" * 64),
        "byte_start": int(row.get("byte_start") or 0),
        "byte_end": int(row.get("byte_end") or 0),
        "score": float(row.get(score_key) or row.get("score") or 0.0),
        "title": row.get("title") or Path(path).stem,
        "summary": row.get("summary") or "",
        "project": row.get("project") or "",
        "timestamp": row.get("timestamp") or "",
    }
    for key in (
        "bm25_score",
        "vector_score",
        "graph_boost",
        "evidence_ids",
        "authority",
        "confidence",
        "status",
        "type",
        "valid_from",
        "valid_to",
        "language",
        "source_id",
        "generation",
        "content",
        "lance_distance",
        "seed_id",
        "hop",
        "direction",
        "edge_type",
        "assertion_path",
        "retrieval_confidence",
    ):
        if key in row:
            hit[key] = row[key]
    if "content" not in hit:
        hit["content"] = hit.get("summary") or ""
    return hit


def retrieve_via_search_memory(
    query: str,
    *,
    scope: str = "all",
    limit: int = 10,
    force_rebuild: bool = False,
    project: str | None = None,
    since: str | None = None,
    as_of: str | None = None,
    semantic: bool = False,
    page_paths: list[Path] | None = None,
    graph: bool = True,
    rerank: bool = True,
    source_tool: str = "search_memory",
    emit_telemetry: bool = True,
    profile: str | None = None,
    catalog: Any = None,
    generation_embedder: object | None = None,
    generation_model_id: str | None = None,
    generation_model_revision: str | None = None,
    deadline_monotonic: float | None = None,
    max_candidates: int | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """Public search path: independent backends → retrieve() → legacy rows."""
    import search_memory

    _check_stopped(deadline_monotonic, cancelled)

    _GenerationSealChanged = GenerationSealChanged

    analysis = analyze_query(query)
    requested = _normalize_profile(profile)
    # semantic=False always forces BASE/lexical regardless of planner profile.
    if not semantic:
        requested = "BASE"
    elif requested is None:
        requested = "HYBRID" if semantic else analysis.recommended_profile
    if requested is None:
        requested = analysis.recommended_profile

    wanted_tuple = PROFILE_SIGNALS[requested]
    if not semantic:
        wanted_tuple = tuple(s for s in wanted_tuple if s != "dense") or ("lexical",)
    hard_deadline = deadline_monotonic is not None

    selected_catalog = catalog if catalog is not None else search_memory._active_generation_catalog()
    corpus_generation = "legacy"
    generation_ctx: dict[str, Any] = {
        "manifest": None,
        "connection": None,
        "graph": None,
        "seal": None,
        "dense_fallback": None,
        "legacy_dense_blocked": False,
    }
    generation_stop = {}
    if deadline_monotonic is not None:
        generation_stop["deadline"] = deadline_monotonic
    if cancelled is not None:
        generation_stop["cancelled"] = cancelled
    optional_generation_stop = {} if hard_deadline else generation_stop

    def _artifact_names_for(manifest: dict[str, object], *, want_vectors: bool) -> tuple[str, ...]:
        names: list[str] = [search_memory.GENERATION_FTS_ARTIFACT]
        if want_vectors and manifest.get("vector_state") == "complete":
            names.extend(search_memory.GENERATION_VECTOR_ARTIFACTS)
        if "graph" in wanted_tuple and search_memory._generation_artifact(
            manifest, "evidence.sqlite3"
        ):
            names.append("evidence.sqlite3")
        return tuple(names)

    def _open_generation(*, want_vectors: bool) -> bool:
        if selected_catalog is None or force_rebuild or page_paths is not None:
            return False
        try:
            from repository_scope import resolve_repository_scope

            repository_scope = resolve_repository_scope(
                search_memory.ROOT,
                deadline=deadline_monotonic,
                cancelled=cancelled,
            )
            manifest = selected_catalog.get_active_for_repository(
                repository_scope, **generation_stop
            )
            if not isinstance(manifest, dict):
                return False
            if want_vectors and manifest.get("vector_state") == "stale":
                generation_ctx["dense_fallback"] = "generation_vectors_unavailable"
                generation_ctx["legacy_dense_blocked"] = True
        except TimeoutError:
            raise
        except Exception:
            return False
        artifact_names = _artifact_names_for(manifest, want_vectors=want_vectors)
        seal = search_memory._generation_consumption_seal(
            selected_catalog, manifest, artifact_names, **generation_stop
        )
        connection = (
            search_memory._generation_connection(
                selected_catalog, manifest, **generation_stop
            )
            if seal is not None
            else None
        )
        if connection is None:
            return False
        generation_ctx["manifest"] = manifest
        generation_ctx["connection"] = connection
        generation_ctx["seal"] = seal
        generation_ctx["artifact_names"] = artifact_names
        if "evidence.sqlite3" in artifact_names:
            try:
                from evidence_graph import EvidenceGraph

                generation_ctx["graph"] = EvidenceGraph.open_active_for_repository(
                    selected_catalog,
                    repository_scope,
                    deadline=deadline_monotonic,
                    cancelled=cancelled,
                )
                if generation_ctx["graph"] is None:
                    connection.close()
                    generation_ctx["connection"] = None
                    return False
            except TimeoutError:
                connection.close()
                generation_ctx["connection"] = None
                raise
            except Exception:
                connection.close()
                generation_ctx["connection"] = None
                generation_ctx["graph"] = None
                return False
        return True

    catalog_requested = (
        selected_catalog is not None and not force_rebuild and page_paths is None
    )
    want_vectors = "dense" in wanted_tuple and semantic
    use_generation = _open_generation(want_vectors=want_vectors)
    generation_fallback: str | None = None
    legacy_fallback: str | None = None
    if use_generation:
        corpus_generation = str(generation_ctx["manifest"]["generation_id"])
    elif catalog_requested:
        # Always continue through retrieve(); report truthful generation failure.
        generation_fallback = "generation_unavailable"

    def lexical_backend(**filters: Any) -> Sequence[Mapping[str, Any]]:
        nonlocal generation_fallback, legacy_fallback, use_generation
        if use_generation:
            try:
                if not search_memory._generation_consumption_unchanged(
                    selected_catalog,
                    generation_ctx["manifest"],
                    generation_ctx["artifact_names"],
                    generation_ctx["seal"],
                    **generation_stop,
                ):
                    generation_fallback = "generation_seal_changed"
                    raise _GenerationSealChanged
                else:
                    use = True
                if use:
                    rows = search_memory._generation_fts_search(
                        filters["query"],
                        generation_ctx["manifest"],
                        generation_ctx["connection"],
                        scope=filters["scope"],
                        limit=filters["limit"],
                        project=filters["project"],
                        since=filters["since"],
                        as_of=filters["as_of"],
                        **generation_stop,
                    )
                    if not search_memory._generation_consumption_unchanged(
                        selected_catalog,
                        generation_ctx["manifest"],
                        generation_ctx["artifact_names"],
                        generation_ctx["seal"],
                        **generation_stop,
                    ):
                        generation_fallback = "generation_seal_changed"
                        raise _GenerationSealChanged
                    else:
                        hits = [_backend_hit_from_legacy(row) for row in rows]
                        return search_memory.apply_hard_filters(
                            hits,
                            project=filters.get("project"),
                            since=filters.get("since"),
                            as_of=filters.get("as_of"),
                            scope=filters.get("scope", "all"),
                        )
            except _GenerationSealChanged:
                raise
            except TimeoutError:
                raise
            except Exception:
                generation_fallback = generation_fallback or "generation_corrupt"
                raise _GenerationSealChanged
        rows = search_memory._legacy_lexical_hits(
            filters["query"],
            scope=filters["scope"],
            limit=filters["limit"],
            force_rebuild=force_rebuild,
            project=filters["project"],
            since=filters["since"],
            as_of=filters["as_of"],
            page_paths=page_paths,
            deadline=deadline_monotonic,
            cancelled=cancelled,
        )
        legacy_fallback = next(
            (
                str(row["fallback_reason"])
                for row in rows
                if row.get("fallback_reason")
            ),
            legacy_fallback,
        )
        hits = [_backend_hit_from_legacy(row) for row in rows]
        return search_memory.apply_hard_filters(
            hits,
            project=filters.get("project"),
            since=filters.get("since"),
            as_of=filters.get("as_of"),
            scope=filters.get("scope", "all"),
        )

    def dense_backend(**filters: Any) -> Sequence[Mapping[str, Any]] | None:
        nonlocal generation_fallback
        if "dense" not in wanted_tuple:
            return None
        if generation_ctx["legacy_dense_blocked"]:
            return None
        if use_generation:
            if generation_fallback in {
                "generation_seal_changed",
                "generation_corrupt",
                "generation_unavailable",
            }:
                generation_ctx["dense_fallback"] = generation_fallback
                return None
            if (
                generation_embedder is None
                or generation_model_id is None
                or generation_model_revision is None
            ):
                generation_ctx["dense_fallback"] = "generation_vectors_unavailable"
                return None
            dense_connection = generation_ctx["connection"]
            owns_dense_connection = False
            try:
                if not search_memory._generation_consumption_unchanged(
                    selected_catalog,
                    generation_ctx["manifest"],
                    generation_ctx["artifact_names"],
                    generation_ctx["seal"],
                    **optional_generation_stop,
                ):
                    generation_ctx["dense_fallback"] = "generation_seal_changed"
                    generation_fallback = "generation_seal_changed"
                    raise _GenerationSealChanged
                if hard_deadline:
                    dense_connection = search_memory._generation_connection(
                        selected_catalog,
                        generation_ctx["manifest"],
                        **optional_generation_stop,
                    )
                    owns_dense_connection = dense_connection is not None
                    if dense_connection is None:
                        generation_ctx["dense_fallback"] = "generation_vectors_unavailable"
                        return None
                rows = search_memory._generation_vectors_search(
                    filters["query"],
                    selected_catalog,
                    generation_ctx["manifest"],
                    dense_connection,
                    embedder=generation_embedder,
                    model_id=generation_model_id,
                    model_revision=generation_model_revision,
                    scope=filters["scope"],
                    limit=filters["limit"],
                    project=filters["project"],
                    since=filters["since"],
                    as_of=filters["as_of"],
                    **optional_generation_stop,
                )
                if rows is None:
                    if not search_memory._generation_consumption_unchanged(
                        selected_catalog,
                        generation_ctx["manifest"],
                        generation_ctx["artifact_names"],
                        generation_ctx["seal"],
                        **optional_generation_stop,
                    ):
                        generation_ctx["dense_fallback"] = "generation_seal_changed"
                        generation_fallback = "generation_seal_changed"
                        raise _GenerationSealChanged
                    generation_ctx["dense_fallback"] = "generation_vectors_unavailable"
                    return None
                if not search_memory._generation_consumption_unchanged(
                    selected_catalog,
                    generation_ctx["manifest"],
                    generation_ctx["artifact_names"],
                    generation_ctx["seal"],
                    **optional_generation_stop,
                ):
                    generation_ctx["dense_fallback"] = "generation_seal_changed"
                    generation_fallback = "generation_seal_changed"
                    raise _GenerationSealChanged
                hits = [
                    _backend_hit_from_legacy({**row, "vector_score": row.get("score")})
                    for row in rows
                ]
                return search_memory.apply_hard_filters(
                    hits,
                    project=filters.get("project"),
                    since=filters.get("since"),
                    as_of=filters.get("as_of"),
                    scope=filters.get("scope", "all"),
                )
            except _GenerationSealChanged:
                raise
            except TimeoutError:
                raise
            except Exception:
                if not search_memory._generation_consumption_unchanged(
                    selected_catalog,
                    generation_ctx["manifest"],
                    generation_ctx["artifact_names"],
                    generation_ctx["seal"],
                    **optional_generation_stop,
                ):
                    generation_ctx["dense_fallback"] = "generation_seal_changed"
                    generation_fallback = "generation_seal_changed"
                    raise _GenerationSealChanged
                generation_ctx["dense_fallback"] = "generation_vectors_unavailable"
                return None
            finally:
                if owns_dense_connection:
                    dense_connection.close()
        # semantic=False / BASE: dense backend not requested via wanted_tuple.
        rows = search_memory._legacy_dense_hits(
            filters["query"],
            scope=filters["scope"],
            limit=filters["limit"],
            project=filters["project"],
            since=filters["since"],
            as_of=filters["as_of"],
            page_paths=page_paths,
            deadline=None if hard_deadline else deadline_monotonic,
            cancelled=None if hard_deadline else cancelled,
        )
        if rows is None:
            return None
        hits = [
            _backend_hit_from_legacy({**row, "vector_score": row.get("score")})
            for row in rows
        ]
        return search_memory.apply_hard_filters(
            hits,
            project=filters.get("project"),
            since=filters.get("since"),
            as_of=filters.get("as_of"),
            scope=filters.get("scope", "all"),
        )

    def graph_backend(**filters: Any) -> Sequence[Mapping[str, Any]] | None:
        if not graph or "graph" not in wanted_tuple:
            return None
        if use_generation:
            active_graph = generation_ctx.get("graph")
            if active_graph is None:
                return None
            if not search_memory._generation_consumption_unchanged(
                selected_catalog,
                generation_ctx["manifest"],
                generation_ctx["artifact_names"],
                generation_ctx["seal"],
                **generation_stop,
            ):
                raise _GenerationSealChanged
            rows = expand_evidence_graph(
                active_graph,
                seeds=filters["seeds"],
                directions=filters["directions"],
                edge_types=filters["edge_types"],
                per_seed_limit=filters["per_seed_limit"],
                global_limit=filters["global_limit"],
                deadline_monotonic=filters["deadline_monotonic"],
                cancelled=cancelled,
            )
            if not search_memory._generation_consumption_unchanged(
                selected_catalog,
                generation_ctx["manifest"],
                generation_ctx["artifact_names"],
                generation_ctx["seal"],
                **generation_stop,
            ):
                raise _GenerationSealChanged
            return rows
        try:
            seed_filters = {
                key: filters[key]
                for key in ("query", "scope", "limit", "project", "since", "as_of")
            }
            seeds = list(lexical_backend(**seed_filters))
            from graph_neighbors import boost_graph_neighbors

            boosts = boost_graph_neighbors(
                [{"path": h["path"], "score": h.get("score", 0)} for h in seeds],
                None,
            )
            return [
                _backend_hit_from_legacy(
                    {
                        "path": item["path"],
                        "candidate_id": item["path"],
                        "score": item.get("graph_boost", 0.0),
                        "graph_boost": item.get("graph_boost", 0.0),
                    }
                )
                for item in boosts
            ]
        except TimeoutError:
            raise
        except Exception:
            return None

    def run_retrieval() -> RetrievalResult:
        return retrieve(
            query,
            requested_profile=requested,
            scope=scope,
            limit=limit,
            project=project,
            since=since,
            as_of=as_of,
            lexical_backend=lexical_backend if "lexical" in wanted_tuple else None,
            dense_backend=dense_backend if ("dense" in wanted_tuple and semantic) else None,
            graph_backend=graph_backend if ("graph" in wanted_tuple and graph) else None,
            corpus_generation=corpus_generation,
            graph_enabled=graph,
            rerank_enabled=rerank,
            partial=False,
            deadline_monotonic=deadline_monotonic,
            max_candidates=max_candidates,
            cancelled=cancelled,
        )

    try:
        try:
            result = run_retrieval()
            if use_generation and not search_memory._generation_consumption_unchanged(
                selected_catalog,
                generation_ctx["manifest"],
                generation_ctx["artifact_names"],
                generation_ctx["seal"],
                **generation_stop,
            ):
                generation_fallback = "generation_seal_changed"
                raise _GenerationSealChanged
        except _GenerationSealChanged:
            use_generation = False
            corpus_generation = "legacy"
            result = run_retrieval()
    finally:
        connection = generation_ctx.get("connection")
        active_graph = generation_ctx.get("graph")
        if active_graph is not None:
            try:
                active_graph.close()
            except Exception:
                pass
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    # Prefer generation-specific dense fallback wording when applicable.
    dense_fallback = generation_ctx.get("dense_fallback") or generation_fallback
    effective_mode = result.trace.effective_mode
    fallback_reason = result.trace.fallback_reason
    if legacy_fallback:
        fallback_reason = legacy_fallback
    elif dense_fallback and fallback_reason in {None, "dense_unavailable"}:
        fallback_reason = str(dense_fallback)
    if generation_fallback and fallback_reason is None:
        fallback_reason = generation_fallback
    partial = result.trace.partial or legacy_fallback is not None

    # Exact filename remains authoritative after optional fusion and reranking.
    if result.candidates and _normalized_filename_stem(
        result.candidates[0].relative_path
    ) == _normalized_filename_stem(query):
        effective_mode = "EXACT"

    if (
        effective_mode != result.trace.effective_mode
        or fallback_reason != result.trace.fallback_reason
        or partial != result.trace.partial
    ):
        result = RetrievalResult(
            candidates=result.candidates,
            trace=RetrievalTrace(
                requested_mode=result.trace.requested_mode,
                effective_mode=effective_mode,
                signals_used=result.trace.signals_used,
                fallback_reason=fallback_reason,
                corpus_generation=result.trace.corpus_generation,
                partial=partial,
                reranker_applied=result.trace.reranker_applied,
                reranker_model_id=result.trace.reranker_model_id,
                reranker_model_revision=result.trace.reranker_model_revision,
                reranker_depth=result.trace.reranker_depth,
                reranker_duration_ms=result.trace.reranker_duration_ms,
                reranker_fallback_reason=result.trace.reranker_fallback_reason,
            ),
            analysis=result.analysis,
            display_meta=result.display_meta,
        )

    rows = candidates_to_legacy(result, display_meta=result.display_meta)
    # Reranking is owned by retrieve(); do not double-apply here.

    if emit_telemetry and rows:
        try:
            from retrieval_telemetry import (
                best_effort_make_event,
                best_effort_record_events,
            )

            events = []
            for rank, item in enumerate(rows, start=1):
                event = best_effort_make_event(
                    event_kind="impression",
                    query=query,
                    retrieval_mode=str(item.get("effective_mode") or "base").lower(),
                    candidate_id=str(
                        item.get("chunk_id")
                        or item.get("slug")
                        or item.get("candidate_id")
                        or Path(str(item.get("path", ""))).stem
                    ),
                    rank=rank,
                    generation=str(item.get("generation") or corpus_generation),
                    source_tool=source_tool,
                )
                if event is not None:
                    events.append(event)
            if events:
                best_effort_record_events(events)
        except Exception:
            pass
    return rows
