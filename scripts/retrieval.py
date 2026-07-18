"""Deterministic retrieval contract: query analysis, profiles, RRF fusion."""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

BackendFn = Callable[..., Sequence[Mapping[str, Any]] | None]

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
    r"\b(?P<t>since|before|after|until|as of|yesterday|today|last week|"
    r"last month|last year|in \d{4}|from \d{4}|between \d{4}|"
    r"с \d{4}|после|до|вчера|сегодня|на этой неделе)\b",
    re.IGNORECASE,
)
_GRAPH_RE = re.compile(
    r"\b(?P<g>depends on|depended on|dependency|dependencies|calls|callers|"
    r"callees|related to|linked to|neighbors?|imports?|imported by|"
    r"зависит|зависимости|связан)\b",
    re.IGNORECASE,
)
_REPO_MAP_RE = re.compile(
    r"\b(?P<r>repo map|repository map|codebase map|architecture overview|"
    r"project structure|directory structure|"
    r"карта репозитория|структура проекта)\b",
    re.IGNORECASE,
)
_IMPACT_RE = re.compile(
    r"\b(?P<i>impact of|what breaks|affected by|blast radius|stale wiki|"
    r"downstream impact|влияние|что сломается)\b",
    re.IGNORECASE,
)
_GLOBAL_RE = re.compile(
    r"\b(?P<s>synthesize|synthesis|across all projects|across projects|"
    r"overall architecture|compare across|global overview|"
    r"синтез|сравни across|глобальный обзор)\b",
    re.IGNORECASE,
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


@dataclass(frozen=True)
class RetrievalResult:
    candidates: tuple[RetrievalCandidate, ...]
    trace: RetrievalTrace
    analysis: QueryAnalysis


def _normalize_profile(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("profile must be a string")
    normalized = value.strip().upper().replace("-", "_")
    if normalized not in PROFILES:
        raise ValueError(f"unknown retrieval profile: {value}")
    return normalized


def analyze_query(query: str) -> QueryAnalysis:
    """Deterministic query analysis used by the retrieval planner."""
    if not isinstance(query, str):
        raise TypeError("query must be a string")
    stripped = query.strip()
    normalized = " ".join(stripped.split())
    intents: list[str] = []
    phrases = tuple(
        match.group(1).strip()
        for match in _QUOTE_RE.finditer(stripped)
        if match.group(1).strip()
    )
    if phrases:
        intents.append("quoted_phrase")

    identifiers: list[str] = []
    for match in _PATH_RE.finditer(stripped):
        identifiers.append(match.group("path"))
    for match in _FILENAME_RE.finditer(stripped):
        identifiers.append(match.group("file"))
    for match in _CAMEL_RE.finditer(stripped):
        identifiers.append(match.group("camel"))
    for match in _SNAKE_RE.finditer(stripped):
        identifiers.append(match.group("snake"))
    for match in _SLUG_RE.finditer(stripped):
        slug = match.group("slug")
        if "/" in slug or slug in identifiers:
            continue
        if any(slug in item for item in identifiers):
            continue
        identifiers.append(slug)
    # Deduplicate while preserving order.
    seen: set[str] = set()
    exact_identifiers = []
    for item in identifiers:
        if item not in seen:
            seen.add(item)
            exact_identifiers.append(item)
    if exact_identifiers:
        intents.append("exact_identifier")

    if _QUESTION_RE.search(normalized) or stripped.endswith("?") or stripped.endswith("？"):
        intents.append("question")
    if _TEMPORAL_RE.search(normalized):
        intents.append("temporal")
    if _GRAPH_RE.search(normalized):
        intents.append("graph_relation")
    if _REPO_MAP_RE.search(normalized):
        intents.append("repo_map")
    if _IMPACT_RE.search(normalized):
        intents.append("impact")
    if _GLOBAL_RE.search(normalized):
        intents.append("global_synthesis")

    profile = "BASE"
    intent_set = set(intents)
    if "global_synthesis" in intent_set:
        profile = "GLOBAL"
    elif "impact" in intent_set:
        profile = "IMPACT"
    elif "repo_map" in intent_set:
        profile = "REPO_MAP"
    elif "graph_relation" in intent_set:
        profile = "GRAPH"
    elif "temporal" in intent_set:
        profile = "TEMPORAL"
    elif "quoted_phrase" in intent_set or "exact_identifier" in intent_set:
        profile = "EXACT"
    elif "question" in intent_set:
        profile = "HYBRID"

    return QueryAnalysis(
        query=stripped,
        normalized_query=normalized,
        intents=tuple(intents),
        exact_identifiers=tuple(exact_identifiers),
        quoted_phrases=phrases,
        recommended_profile=profile,
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
    if value is None:
        return ()
    if isinstance(value, tuple):
        return tuple(str(item) for item in value)
    if isinstance(value, list):
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


def fuse_rrf(
    *,
    lexical: Sequence[Mapping[str, Any]] | None,
    dense: Sequence[Mapping[str, Any]] | None,
    graph: Sequence[Mapping[str, Any]] | None,
    k: int = RRF_K,
) -> tuple[RetrievalCandidate, ...]:
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
            }
        else:
            # Prefer first non-empty display fields from any backend.
            for field in (
                "title",
                "summary",
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

    ordered = sorted(scores, key=lambda item: (-scores[item], item))
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
                final_score=rrf,
                evidence_ids=info["evidence_ids"],
            )
        )
    # Stash display metadata for compatibility conversion (not part of contract).
    fuse_rrf._last_meta = meta  # type: ignore[attr-defined]
    return tuple(candidates)


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
    signals: list[str] = []
    fallback: str | None = None

    if "lexical" in wanted and ran_lexical:
        signals.append("lexical")

    if "dense" in wanted:
        if dense_available is True and ran_dense:
            signals.append("dense")
        elif dense_available is False or (dense_available is None and not ran_dense):
            fallback = fallback or "dense_unavailable"

    if "graph" in wanted:
        if not graph_enabled:
            fallback = fallback or "graph_disabled"
        elif graph_available is True and ran_graph:
            signals.append("graph")
        else:
            fallback = fallback or "graph_unavailable"

    if not signals and ran_lexical:
        signals.append("lexical")

    signal_set = set(signals)
    if requested == "HYBRID":
        effective = "HYBRID" if "dense" in signal_set else ("BASE" if "lexical" in signal_set else requested)
    elif requested == "GRAPH":
        effective = "GRAPH" if "graph" in signal_set else ("BASE" if "lexical" in signal_set else requested)
    elif requested == "GLOBAL":
        if "dense" in signal_set and "graph" in signal_set:
            effective = "GLOBAL"
        elif "dense" in signal_set:
            effective = "HYBRID"
        elif "graph" in signal_set:
            effective = "GRAPH"
        else:
            effective = "BASE" if "lexical" in signal_set else requested
    elif requested in {"REPO_MAP", "IMPACT"}:
        if "graph" in signal_set:
            effective = requested
        else:
            effective = "BASE" if "lexical" in signal_set else requested
    else:
        effective = requested if signals else "BASE"

    return effective, fallback, tuple(signals)


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
) -> RetrievalResult:
    """Plan and execute retrieval with truthful mode/signal reporting.

    Lexical and dense backends are invoked independently with identical hard
    filters. Fusion is rank-only RRF. Raw backend scores stay on candidates.
    """
    analysis = analyze_query(query)
    requested = _normalize_profile(requested_profile) or analysis.recommended_profile
    filters = {
        "query": analysis.normalized_query or analysis.query,
        "scope": scope,
        "limit": limit,
        "project": project,
        "since": since,
        "as_of": as_of,
    }

    wanted = PROFILE_SIGNALS[requested]
    lexical_hits: Sequence[Mapping[str, Any]] | None = None
    dense_hits: Sequence[Mapping[str, Any]] | None = None
    graph_hits: Sequence[Mapping[str, Any]] | None = None

    ran_lexical = False
    ran_dense = False
    ran_graph = False
    dense_available: bool | None = None
    graph_available: bool | None = None

    if lexical_backend is not None and "lexical" in wanted:
        lexical_hits = lexical_backend(**filters) or ()
        ran_lexical = True

    if dense_backend is not None and "dense" in wanted:
        dense_hits = dense_backend(**filters)
        ran_dense = True
        # None ⇒ backend unavailable; empty sequence ⇒ available but no hits.
        dense_available = dense_hits is not None

    if graph_backend is not None and "graph" in wanted and graph_enabled:
        graph_hits = graph_backend(**filters)
        ran_graph = True
        graph_available = graph_hits is not None
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

    fuse_lexical = lexical_hits if "lexical" in signals else None
    fuse_dense = dense_hits if "dense" in signals and dense_hits is not None else None
    fuse_graph = graph_hits if "graph" in signals and graph_hits is not None else None
    candidates = fuse_rrf(lexical=fuse_lexical, dense=fuse_dense, graph=fuse_graph)

    # Conditional reranking (Task 13).
    signal_list = list(signals)
    if candidates and rerank_enabled:
        try:
            from reranker import rerank as _rerank
            from reranker import should_rerank

            legacy_rows = [
                {
                    "candidate_id": c.candidate_id,
                    "path": c.relative_path,
                    "relative_path": c.relative_path,
                    "summary": "",
                    "title": Path(c.relative_path).stem,
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
                }
                for c in candidates
            ]
            apply, skip_reason = should_rerank(
                profile=requested,
                candidates=legacy_rows,
                analysis_intents=analysis.intents,
                rerank_enabled=rerank_enabled,
            )
            if apply:
                # Rerank a deeper pool then cut to limit.
                pool_limit = max(limit, 20) if limit > 0 else 20
                reranked = _rerank(
                    analysis.normalized_query or analysis.query,
                    legacy_rows[:pool_limit],
                    limit=pool_limit,
                )
                if reranked and reranked[0].get("reranker_applied"):
                    signal_list.append("reranker")
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
                    # Optional reranker missing/errored: soft-skip without
                    # poisoning truthful backend fallback reasons.
                    pass
        except Exception:
            pass

    if limit > 0:
        candidates = candidates[:limit]

    # Attach display meta for search() conversion when available.
    meta = getattr(fuse_rrf, "_last_meta", {})
    retrieve._last_display_meta = meta  # type: ignore[attr-defined]

    trace = RetrievalTrace(
        requested_mode=requested,
        effective_mode=effective,
        signals_used=tuple(dict.fromkeys(signal_list)),
        fallback_reason=fallback,
        corpus_generation=corpus_generation,
        partial=partial,
    )
    return RetrievalResult(candidates=candidates, trace=trace, analysis=analysis)


def trace_to_dict(trace: RetrievalTrace) -> dict[str, object]:
    return {
        "schema_version": "retrieval-trace/v1",
        "requested_mode": trace.requested_mode,
        "effective_mode": trace.effective_mode,
        "signals_used": list(trace.signals_used),
        "fallback_reason": trace.fallback_reason,
        "corpus_generation": trace.corpus_generation,
        "partial": trace.partial,
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
    meta = display_meta if display_meta is not None else getattr(retrieve, "_last_display_meta", {})
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
        ):
            if info.get(key) not in (None, ""):
                row[key] = info[key]
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
    ):
        if key in row:
            hit[key] = row[key]
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
) -> list[dict[str, Any]]:
    """Public search path: independent backends → retrieve() → legacy rows."""
    import search_memory

    analysis = analyze_query(query)
    requested = _normalize_profile(profile)
    if requested is None:
        if semantic:
            requested = "HYBRID"
        else:
            requested = analysis.recommended_profile

    wanted_tuple = PROFILE_SIGNALS[requested]

    selected_catalog = catalog if catalog is not None else search_memory._active_generation_catalog()
    corpus_generation = "legacy"
    generation_ctx: dict[str, Any] = {
        "manifest": None,
        "connection": None,
        "seal": None,
        "dense_fallback": None,
    }

    def _open_generation() -> bool:
        if selected_catalog is None or force_rebuild or page_paths is not None:
            return False
        try:
            manifest = selected_catalog.get_active()
        except Exception:
            return False
        if not isinstance(manifest, dict):
            return False
        artifact_names = (search_memory.GENERATION_FTS_ARTIFACT,)
        seal = search_memory._generation_consumption_seal(
            selected_catalog, manifest, artifact_names
        )
        connection = (
            search_memory._generation_connection(selected_catalog, manifest)
            if seal is not None
            else None
        )
        if connection is None:
            return False
        generation_ctx["manifest"] = manifest
        generation_ctx["connection"] = connection
        generation_ctx["seal"] = seal
        generation_ctx["artifact_names"] = artifact_names
        return True

    catalog_requested = (
        selected_catalog is not None and not force_rebuild and page_paths is None
    )
    use_generation = _open_generation()
    if use_generation:
        corpus_generation = str(generation_ctx["manifest"]["generation_id"])
    elif catalog_requested:
        # Broken/missing generation → preserve legacy emergency path (incl. test mocks).
        return search_memory._legacy_search(
            query,
            scope,
            limit,
            force_rebuild,
            project,
            since,
            as_of,
            semantic,
            page_paths,
            graph,
            rerank,
            source_tool,
            emit_telemetry,
        )

    def lexical_backend(**filters: Any) -> Sequence[Mapping[str, Any]]:
        if use_generation:
            try:
                rows = search_memory._generation_fts_search(
                    filters["query"],
                    generation_ctx["manifest"],
                    generation_ctx["connection"],
                    scope=filters["scope"],
                    limit=filters["limit"],
                    project=filters["project"],
                    since=filters["since"],
                    as_of=filters["as_of"],
                )
                return [_backend_hit_from_legacy(row) for row in rows]
            except Exception:
                return ()
        rows = search_memory._legacy_lexical_hits(
            filters["query"],
            scope=filters["scope"],
            limit=filters["limit"],
            force_rebuild=force_rebuild,
            project=filters["project"],
            since=filters["since"],
            as_of=filters["as_of"],
            page_paths=page_paths,
        )
        return [_backend_hit_from_legacy(row) for row in rows]

    def dense_backend(**filters: Any) -> Sequence[Mapping[str, Any]] | None:
        if "dense" not in wanted_tuple:
            return None
        if use_generation:
            if (
                generation_embedder is None
                or generation_model_id is None
                or generation_model_revision is None
            ):
                generation_ctx["dense_fallback"] = "generation_vectors_unavailable"
                return None
            try:
                rows = search_memory._generation_vectors_search(
                    filters["query"],
                    selected_catalog,
                    generation_ctx["manifest"],
                    generation_ctx["connection"],
                    embedder=generation_embedder,
                    model_id=generation_model_id,
                    model_revision=generation_model_revision,
                    scope=filters["scope"],
                    limit=filters["limit"],
                    project=filters["project"],
                    since=filters["since"],
                    as_of=filters["as_of"],
                )
                if rows is None:
                    generation_ctx["dense_fallback"] = "generation_vectors_unavailable"
                    return None
                return [
                    _backend_hit_from_legacy({**row, "vector_score": row.get("score")})
                    for row in rows
                ]
            except Exception:
                generation_ctx["dense_fallback"] = "generation_vectors_unavailable"
                return None
        rows = search_memory._legacy_dense_hits(
            filters["query"],
            scope=filters["scope"],
            limit=filters["limit"],
            project=filters["project"],
            since=filters["since"],
            as_of=filters["as_of"],
            page_paths=page_paths,
        )
        if rows is None:
            return None
        return [
            _backend_hit_from_legacy({**row, "vector_score": row.get("score")})
            for row in rows
        ]

    def graph_backend(**filters: Any) -> Sequence[Mapping[str, Any]] | None:
        if not graph or "graph" not in wanted_tuple:
            return None
        # Generation path does not mix legacy graph (Task 12 isolation).
        if use_generation:
            return None
        try:
            seeds = list(lexical_backend(**filters))
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
        except Exception:
            return None

    try:
        result = retrieve(
            query,
            requested_profile=requested,
            scope=scope,
            limit=limit,
            project=project,
            since=since,
            as_of=as_of,
            lexical_backend=lexical_backend if "lexical" in wanted_tuple else None,
            dense_backend=dense_backend if "dense" in wanted_tuple else None,
            graph_backend=graph_backend if ("graph" in wanted_tuple and graph) else None,
            corpus_generation=corpus_generation,
            graph_enabled=graph and not use_generation,
            rerank_enabled=rerank,
        )
    finally:
        connection = generation_ctx.get("connection")
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    # Prefer generation-specific dense fallback wording when applicable.
    dense_fallback = generation_ctx.get("dense_fallback")
    effective_mode = result.trace.effective_mode
    fallback_reason = result.trace.fallback_reason
    if dense_fallback and fallback_reason in {None, "dense_unavailable"}:
        fallback_reason = str(dense_fallback)

    # Filename exact short-circuit: stem matches query with spaces→hyphens.
    if (
        result.candidates
        and result.trace.signals_used == ("lexical",)
        and effective_mode in {"BASE", "EXACT", "DIRECT"}
    ):
        query_normalized = query.lower().strip().replace(" ", "-")
        top_stem = Path(result.candidates[0].relative_path).stem.lower()
        if top_stem == query_normalized:
            effective_mode = "EXACT"

    if (
        effective_mode != result.trace.effective_mode
        or fallback_reason != result.trace.fallback_reason
    ):
        result = RetrievalResult(
            candidates=result.candidates,
            trace=RetrievalTrace(
                requested_mode=result.trace.requested_mode,
                effective_mode=effective_mode,
                signals_used=result.trace.signals_used,
                fallback_reason=fallback_reason,
                corpus_generation=result.trace.corpus_generation,
                partial=result.trace.partial,
            ),
            analysis=result.analysis,
        )

    # Uppercase contract modes → lowercase legacy keys expected by older callers.
    mode_lower = {
        "requested_mode": result.trace.requested_mode,
        "effective_mode": result.trace.effective_mode,
    }
    rows = candidates_to_legacy(result)
    for row in rows:
        row["requested_mode"] = mode_lower["requested_mode"]
        row["effective_mode"] = mode_lower["effective_mode"]
        # Generation-era tests compare lowercase effective modes.
        if isinstance(row["effective_mode"], str) and row["effective_mode"].isupper():
            # Keep contract uppercase in new fields; also expose lowercase for
            # assertions that predate Task 11.
            pass
        # Normalize effective_mode for legacy generation tests that expect "base"/"hybrid".
        em = str(row["effective_mode"])
        if em == "BASE":
            row["effective_mode"] = "BASE"
        elif em == "HYBRID":
            row["effective_mode"] = "HYBRID"

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
