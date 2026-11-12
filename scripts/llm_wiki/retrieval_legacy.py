"""Conversion of retrieval candidates to and from the legacy search hit shape."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from llm_wiki.retrieval_query import (
    RetrievalCandidate,
    RetrievalResult,
    RetrievalTrace,
    first_present,
    source_sha256,
)

_LEGACY_DISPLAY_FIELDS = (
    "authority",
    "confidence",
    "status",
    "type",
    "valid_from",
    "valid_to",
    "language",
    "source_id",
    "content",
    "graph_seed_id",
    "graph_direction",
    "graph_edge_type",
    "graph_text_overlap",
)


def _legacy_trace_fields(trace: RetrievalTrace) -> dict[str, Any]:
    return {
        "requested_mode": trace.requested_mode,
        "effective_mode": trace.effective_mode,
        "signals_used": list(trace.signals_used),
        "fallback_reason": trace.fallback_reason,
        "generation": trace.corpus_generation,
        "partial": trace.partial,
        "reranker_applied": trace.reranker_applied,
        "reranker_model_id": trace.reranker_model_id,
        "reranker_model_revision": trace.reranker_model_revision,
        "reranker_depth": trace.reranker_depth,
        "reranker_duration_ms": trace.reranker_duration_ms,
        "reranker_fallback_reason": trace.reranker_fallback_reason,
        "signals_requested": list(trace.signals_requested),
    }


def _legacy_scores(candidate: RetrievalCandidate) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
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
        "score": round(candidate.final_score, 4),
    }


def _display_value(
    override: Mapping[str, str], info: Mapping[str, Any], path: str, key: str, fallback: str
) -> str:
    """Caller-supplied text wins, then the candidate's own metadata."""
    return override.get(path) or info.get(key) or fallback


def _legacy_display(
    path: str,
    info: Mapping[str, Any],
    overrides: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    return {
        "path": path,
        "title": _display_value(overrides["titles"], info, path, "title", Path(path).stem),
        "summary": _display_value(overrides["summaries"], info, path, "summary", ""),
        "project": _display_value(overrides["projects"], info, path, "project", ""),
        "timestamp": _display_value(overrides["timestamps"], info, path, "timestamp", ""),
    }


def _legacy_assertion_path(info: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    steps = info.get("assertion_path")
    if not steps:
        return None
    return [
        {**dict(step), "evidence_ids": list(step.get("evidence_ids") or ())}
        for step in steps
    ]


def _legacy_extras(
    candidate: RetrievalCandidate, info: Mapping[str, Any]
) -> dict[str, Any]:
    extras: dict[str, Any] = {
        key: info[key]
        for key in _LEGACY_DISPLAY_FIELDS
        if info.get(key) not in (None, "")
    }
    assertion_path = _legacy_assertion_path(info)
    if assertion_path is not None:
        extras["assertion_path"] = assertion_path
    if candidate.evidence_ids:
        extras["evidence_ids"] = list(candidate.evidence_ids)
    return extras


def _display_source(
    display_meta: Mapping[str, Mapping[str, Any]] | None, result: RetrievalResult
) -> Mapping[str, Mapping[str, Any]]:
    if display_meta is not None:
        return display_meta
    return result.display_meta or {}


def _display_overrides(
    titles: Mapping[str, str] | None,
    summaries: Mapping[str, str] | None,
    projects: Mapping[str, str] | None,
    timestamps: Mapping[str, str] | None,
) -> dict[str, Mapping[str, str]]:
    return {
        "titles": titles or {},
        "summaries": summaries or {},
        "projects": projects or {},
        "timestamps": timestamps or {},
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
    overrides = _display_overrides(titles, summaries, projects, timestamps)
    meta = _display_source(display_meta, result)
    trace_fields = _legacy_trace_fields(result.trace)
    return [
        _legacy_row(candidate, _candidate_info(meta, candidate), overrides, trace_fields)
        for candidate in result.candidates
    ]


def _candidate_info(meta: object, candidate: RetrievalCandidate) -> Mapping[str, Any]:
    if not isinstance(meta, dict):
        return {}
    return meta.get(candidate.candidate_id, {})


def _legacy_row(
    candidate: RetrievalCandidate,
    info: Mapping[str, Any],
    overrides: Mapping[str, Mapping[str, str]],
    trace_fields: Mapping[str, Any],
) -> dict[str, Any]:
    row = _legacy_display(candidate.relative_path, info, overrides)
    row.update(_legacy_scores(candidate))
    row["chunk_id"] = info.get("chunk_id") or candidate.candidate_id
    row.update(trace_fields)
    row.update(_legacy_extras(candidate, info))
    return row


# Fields a backend may supply that pass through untouched when present.
_PASSTHROUGH_HIT_FIELDS = (
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
    "seed_id",
    "hop",
    "direction",
    "edge_type",
    "assertion_path",
    "retrieval_confidence",
)


def _legacy_candidate_id(row: Mapping[str, Any], path: str) -> str:
    import generation_search

    fallback = generation_search.legacy_candidate_id(path) or path
    return str(first_present(row, ("candidate_id", "chunk_id", "slug"), fallback))


def _base_hit(row: Mapping[str, Any], path: str, score_key: str) -> dict[str, Any]:
    candidate_id = _legacy_candidate_id(row, path)
    return {
        "candidate_id": candidate_id,
        "chunk_id": first_present(row, ("chunk_id",), candidate_id),
        "parent_id": str(first_present(row, ("parent_id", "parent_page"), path)),
        "relative_path": path,
        "path": path,
        "heading_path": first_present(row, ("heading_path", "heading_ancestry"), ()),
        "source_sha256": source_sha256(row),
        "byte_start": int(first_present(row, ("byte_start",), 0)),
        "byte_end": int(first_present(row, ("byte_end",), 0)),
        "score": float(first_present(row, (score_key, "score"), 0.0)),
        "title": first_present(row, ("title",), Path(path).stem),
        "summary": first_present(row, ("summary",), ""),
        "project": first_present(row, ("project",), ""),
        "timestamp": first_present(row, ("timestamp",), ""),
    }


def backend_hit_from_legacy(
    row: Mapping[str, Any], *, score_key: str = "score"
) -> dict[str, Any]:
    path = str(first_present(row, ("path", "relative_path"), ""))
    hit = _base_hit(row, path, score_key)
    hit.update({key: row[key] for key in _PASSTHROUGH_HIT_FIELDS if key in row})
    hit.setdefault("content", hit.get("summary") or "")
    return hit
