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
    r"(?P<path>(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.(?:md|py|ts|tsx|js|jsx|rs|go|java|cs))"
)
_SLUG_RE = re.compile(r"\b(?P<slug>[a-z0-9]+(?:-[a-z0-9]+){1,12})\b")
_CAMEL_RE = re.compile(r"\b(?P<camel>[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b")
_QUESTION_RE = re.compile(
    r"^(?P<q>who|what|when|where|why|how|which|does|do|is|are|can|should)\b",
    re.IGNORECASE,
)
_TEMPORAL_RE = re.compile(
    r"\b(?P<t>since|before|after|until|as of|yesterday|today|last week|"
    r"last month|last year|in \d{4}|from \d{4}|between \d{4})\b",
    re.IGNORECASE,
)
_GRAPH_RE = re.compile(
    r"\b(?P<g>depends on|depended on|dependency|dependencies|calls|callers|"
    r"callees|related to|linked to|neighbors?|imports?|imported by)\b",
    re.IGNORECASE,
)
_REPO_MAP_RE = re.compile(
    r"\b(?P<r>repo map|repository map|codebase map|architecture overview|"
    r"project structure|directory structure)\b",
    re.IGNORECASE,
)
_IMPACT_RE = re.compile(
    r"\b(?P<i>impact of|what breaks|affected by|blast radius|stale wiki|"
    r"downstream impact)\b",
    re.IGNORECASE,
)
_GLOBAL_RE = re.compile(
    r"\b(?P<s>synthesize|synthesis|across all projects|across projects|"
    r"overall architecture|compare across|global overview)\b",
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
    vector_distance: float | None
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
    phrases = tuple(match.group(1).strip() for match in _QUOTE_RE.finditer(stripped) if match.group(1).strip())
    if phrases:
        intents.append("quoted_phrase")

    identifiers: list[str] = []
    for match in _PATH_RE.finditer(stripped):
        identifiers.append(match.group("path"))
    for match in _CAMEL_RE.finditer(stripped):
        identifiers.append(match.group("camel"))
    # Prefer multi-segment slugs; skip if already captured as a path component.
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

    if stripped.endswith("?") or _QUESTION_RE.search(normalized):
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
    """Fuse independent ranked lists with weighted RRF. Larger final_score wins."""
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
                "vector_distance": None,
                "graph_rank": None,
                "graph_score": None,
                "evidence_ids": tuple(
                    str(item)
                    for item in (row.get("evidence_ids") or ())
                    if isinstance(item, str) and item
                ),
            }
        return key

    if lexical:
        for rank, row in enumerate(lexical, start=1):
            key = ensure(row)
            scores[key] = scores.get(key, 0.0) + BM25_WEIGHT / (k + rank)
            meta[key]["bm25_rank"] = rank
            meta[key]["bm25_score"] = _as_float(row.get("score") if "score" in row else row.get("bm25_score"))

    if dense:
        for rank, row in enumerate(dense, start=1):
            key = ensure(row)
            scores[key] = scores.get(key, 0.0) + DENSE_WEIGHT / (k + rank)
            meta[key]["vector_rank"] = rank
            meta[key]["vector_score"] = _as_float(
                row.get("vector_score") if "vector_score" in row else row.get("score")
            )
            meta[key]["vector_distance"] = _as_float(
                row.get("distance") if "distance" in row else row.get("vector_distance")
            )

    if graph:
        for rank, row in enumerate(graph, start=1):
            key = ensure(row)
            boost = _as_float(row.get("graph_boost") if "graph_boost" in row else row.get("score")) or 0.0
            scores[key] = scores.get(key, 0.0) + GRAPH_WEIGHT * max(boost, 0.0) / (k * 2 + rank)
            meta[key]["graph_rank"] = rank
            meta[key]["graph_score"] = boost

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
                vector_distance=info["vector_distance"],
                graph_rank=info["graph_rank"],
                graph_score=info["graph_score"],
                rrf_score=rrf,
                rerank_score=None,
                final_score=rrf,
                evidence_ids=info["evidence_ids"],
            )
        )
    return tuple(candidates)


def _resolve_effective_mode(
    requested: str,
    *,
    has_lexical: bool,
    has_dense: bool,
    has_graph: bool,
    graph_enabled: bool,
) -> tuple[str, str | None, tuple[str, ...]]:
    wanted = PROFILE_SIGNALS[requested]
    signals: list[str] = []
    fallback: str | None = None

    if "lexical" in wanted and has_lexical:
        signals.append("lexical")
    if "dense" in wanted:
        if has_dense:
            signals.append("dense")
        else:
            fallback = "dense_unavailable"
    if "graph" in wanted:
        if not graph_enabled:
            fallback = fallback or "graph_disabled"
        elif has_graph:
            signals.append("graph")
        else:
            fallback = fallback or "graph_unavailable"

    if not signals and has_lexical:
        signals.append("lexical")

    if requested == "HYBRID" and "dense" not in signals:
        effective = "BASE" if "lexical" in signals else requested
    elif requested == "GRAPH" and "graph" not in signals:
        effective = "BASE" if "lexical" in signals else requested
    elif requested in {"REPO_MAP", "IMPACT", "GLOBAL"} and "graph" not in signals and "dense" not in signals:
        effective = "BASE" if "lexical" in signals else requested
    elif requested == "GLOBAL" and "dense" not in signals and "graph" in signals:
        effective = "GRAPH"
    elif requested == "GLOBAL" and "dense" in signals and "graph" not in signals:
        effective = "HYBRID"
    elif requested == "HYBRID" and "dense" in signals:
        effective = "HYBRID"
    elif requested == "GRAPH" and "graph" in signals:
        effective = "GRAPH"
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
    """Plan and execute retrieval with truthful mode/signal reporting."""
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

    lexical_hits: Sequence[Mapping[str, Any]] | None = None
    dense_hits: Sequence[Mapping[str, Any]] | None = None
    graph_hits: Sequence[Mapping[str, Any]] | None = None

    wanted = PROFILE_SIGNALS[requested]
    if lexical_backend is not None and "lexical" in wanted:
        lexical_hits = lexical_backend(**filters) or ()
    if dense_backend is not None and "dense" in wanted:
        dense_hits = dense_backend(**filters)
    if graph_backend is not None and "graph" in wanted and graph_enabled:
        graph_hits = graph_backend(**filters)

    has_lexical = bool(lexical_hits)
    has_dense = dense_hits is not None and len(dense_hits) > 0
    has_graph = graph_hits is not None and len(graph_hits) > 0

    # Dense backend may explicitly return None to mean unavailable.
    dense_available = dense_hits is not None
    graph_available = graph_hits is not None

    effective, fallback, signals = _resolve_effective_mode(
        requested,
        has_lexical=has_lexical or (lexical_backend is not None and "lexical" in wanted),
        has_dense=has_dense if dense_available else False,
        has_graph=has_graph if graph_available else False,
        graph_enabled=graph_enabled,
    )

    # When dense backend returns None, treat as unavailable even if empty list differs.
    if "dense" in wanted and dense_backend is not None and dense_hits is None:
        fallback = fallback or "dense_unavailable"
        if effective == "HYBRID":
            effective = "BASE"
        signals = tuple(signal for signal in signals if signal != "dense")
        if "lexical" not in signals and (lexical_hits is not None or lexical_backend is not None):
            signals = ("lexical",) + signals

    if "graph" in wanted and not graph_enabled:
        fallback = fallback or "graph_disabled"
        if effective == "GRAPH":
            effective = "BASE"
        signals = tuple(signal for signal in signals if signal != "graph")

    fuse_lexical = lexical_hits if "lexical" in signals else None
    fuse_dense = dense_hits if "dense" in signals else None
    fuse_graph = graph_hits if "graph" in signals else None
    candidates = fuse_rrf(lexical=fuse_lexical, dense=fuse_dense, graph=fuse_graph)
    if limit > 0:
        candidates = candidates[:limit]

    # Reranker is reported only when Task 13 enables real conditional invocation.
    # Keep the flag accepted so CLI/API surface is stable.
    _ = rerank_enabled

    trace = RetrievalTrace(
        requested_mode=requested,
        effective_mode=effective,
        signals_used=signals,
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
) -> list[dict[str, Any]]:
    """Convert orchestrator output to the historical search() dict rows."""
    title_map = titles or {}
    summary_map = summaries or {}
    project_map = projects or {}
    timestamp_map = timestamps or {}
    rows: list[dict[str, Any]] = []
    for candidate in result.candidates:
        path = candidate.relative_path
        rows.append(
            {
                "path": path,
                "title": title_map.get(path, Path(path).stem),
                "summary": summary_map.get(path, ""),
                "score": round(candidate.final_score, 4),
                "project": project_map.get(path, ""),
                "timestamp": timestamp_map.get(path, ""),
                "candidate_id": candidate.candidate_id,
                "chunk_id": candidate.candidate_id,
                "source_sha256": candidate.source_sha256,
                "heading_ancestry": list(candidate.heading_path),
                "bm25_rank": candidate.bm25_rank,
                "bm25_score": candidate.bm25_score,
                "vector_rank": candidate.vector_rank,
                "vector_score": candidate.vector_score,
                "vector_distance": candidate.vector_distance,
                "graph_rank": candidate.graph_rank,
                "graph_score": candidate.graph_score,
                "rrf_score": candidate.rrf_score,
                "rerank_score": candidate.rerank_score,
                "final_score": candidate.final_score,
                "requested_mode": result.trace.requested_mode,
                "effective_mode": result.trace.effective_mode,
                "signals_used": list(result.trace.signals_used),
                "fallback_reason": result.trace.fallback_reason,
                "generation": result.trace.corpus_generation,
            }
        )
    return rows


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
    """Compatibility path: plan profile, then reuse search_memory backends."""
    import search_memory

    analysis = analyze_query(query)
    requested = _normalize_profile(profile)
    if requested is None:
        if semantic:
            requested = "HYBRID"
        else:
            requested = analysis.recommended_profile

    # Use existing search backends for work; annotate contract fields in place.
    use_semantic = semantic or requested in {"HYBRID", "GLOBAL"}
    use_graph = graph and requested in {"GRAPH", "REPO_MAP", "IMPACT", "GLOBAL", "HYBRID"}
    if requested in {"EXACT", "BASE", "DIRECT", "TEMPORAL", "CACHED_FULL"}:
        use_semantic = bool(semantic) if requested == "TEMPORAL" else False
        use_graph = bool(graph) if requested == "TEMPORAL" else False

    selected_catalog = catalog if catalog is not None else search_memory._active_generation_catalog()
    rows = search_memory._search_backends(
        query,
        scope=scope,
        limit=limit,
        force_rebuild=force_rebuild,
        project=project,
        since=since,
        as_of=as_of,
        semantic=use_semantic,
        page_paths=page_paths,
        graph=use_graph and graph,
        rerank=rerank,
        source_tool=source_tool,
        emit_telemetry=False,
        catalog=selected_catalog,
        generation_embedder=generation_embedder,
        generation_model_id=generation_model_id,
        generation_model_revision=generation_model_revision,
    )
    if not rows:
        return []

    generation = str(rows[0].get("generation") or "legacy")
    sample_mode = str(rows[0].get("effective_mode") or "").lower()
    backend_fallback = rows[0].get("fallback_reason")
    fallback = backend_fallback if isinstance(backend_fallback, str) and backend_fallback else None

    signals_hint: list[str]
    if sample_mode in {"bm25", "base", "exact"}:
        signals_hint = ["lexical"]
    elif sample_mode == "hybrid":
        signals_hint = ["lexical", "dense"]
        if use_graph and graph:
            signals_hint.append("graph")
    else:
        signals_hint = ["lexical"]
        if use_semantic and sample_mode not in {"", "base", "bm25", "exact"}:
            signals_hint.append("dense")
        if use_graph and graph:
            signals_hint.append("graph")

    if fallback in {"generation_vectors_unavailable", "dense_unavailable"}:
        signals_hint = [signal for signal in signals_hint if signal != "dense"]

    mode_map = {
        "exact": "EXACT",
        "hybrid": "HYBRID",
        "base": "BASE",
        "bm25": "BASE",
        "direct": "DIRECT",
        "graph": "GRAPH",
        "temporal": "TEMPORAL",
        "repo_map": "REPO_MAP",
        "impact": "IMPACT",
        "global": "GLOBAL",
        "cached_full": "CACHED_FULL",
    }
    if sample_mode in mode_map:
        effective = mode_map[sample_mode]
        if sample_mode == "hybrid" and "dense" not in signals_hint:
            effective = "BASE"
    elif requested in {"DIRECT", "EXACT", "BASE", "TEMPORAL", "CACHED_FULL"}:
        effective = requested
    elif requested == "HYBRID" and "dense" not in signals_hint:
        effective = "BASE"
        fallback = fallback or "dense_unavailable"
    elif requested == "GRAPH" and "graph" not in signals_hint:
        effective = "BASE"
        fallback = fallback or ("graph_disabled" if not graph else "graph_unavailable")
    else:
        effective = requested

    # Filename short-circuit and other backend-exact hits keep EXACT even when the
    # planner recommended BASE for a plain natural-language query.
    if sample_mode == "exact":
        effective = "EXACT"

    if not graph and requested in {"GRAPH", "REPO_MAP", "IMPACT"}:
        fallback = fallback or "graph_disabled"
        if effective == requested:
            effective = "BASE"

    signals = list(dict.fromkeys(signals_hint)) or ["lexical"]
    for item in rows:
        score = float(item.get("fused_score") or item.get("score") or 0.0)
        item["score"] = round(score, 4)
        item["rrf_score"] = round(float(item.get("fused_score") or score), 4)
        item["final_score"] = item["rrf_score"]
        item["requested_mode"] = requested
        item["effective_mode"] = effective
        item["signals_used"] = list(signals)
        item["fallback_reason"] = fallback
        item["generation"] = str(item.get("generation") or generation)
        if not item.get("candidate_id"):
            item["candidate_id"] = str(
                item.get("chunk_id")
                or item.get("slug")
                or Path(str(item.get("path") or "")).stem
            )

    if emit_telemetry:
        try:
            from retrieval_telemetry import (
                best_effort_make_event,
                best_effort_record_events,
            )

            events = []
            for rank, result in enumerate(rows, start=1):
                event = best_effort_make_event(
                    event_kind="impression",
                    query=query,
                    retrieval_mode=str(result["effective_mode"]).lower(),
                    candidate_id=str(
                        result.get("chunk_id")
                        or result.get("slug")
                        or result.get("candidate_id")
                        or Path(str(result.get("path", ""))).stem
                    ),
                    rank=rank,
                    generation=str(result["generation"]),
                    source_tool=source_tool,
                )
                if event is not None:
                    events.append(event)
            if events:
                best_effort_record_events(events)
        except Exception:
            pass
    return rows
