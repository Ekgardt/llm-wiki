"""Reranking of fused candidates, per source and per breadcrumb group."""
from __future__ import annotations

import time
from collections.abc import (
    Callable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from llm_wiki import retrieval_optional_stages
from llm_wiki.retrieval_fusion import evidence_ids_of
from llm_wiki.retrieval_optional_stages import (
    OPTIONAL_STAGE_TAIL_RESERVE_SECONDS,
    OptionalStageNotAdmitted,
    OptionalStageTimeout,
    observed_cost_is_stale,
    observed_optional_stage_cost,
    optional_stage_deadline,
    optional_stage_fits,
    require_optional_deadline,
    require_optional_not_cancelled,
    run_optional_bounded,
)
from llm_wiki.retrieval_query import (
    QueryAnalysis,
    RetrievalCandidate,
    as_float,
    as_int,
    check_stopped,
    first_present,
    heading_path_of,
    note_generation_degradation,
    source_sha256,
)


@dataclass
class RerankTrace:
    """What the reranking stage did, for the truthful retrieval trace."""

    applied: bool = False
    model_id: object = None
    model_revision: object = None
    depth: object = None
    duration_ms: object = None
    fallback_reason: str | None = None
    optional_timeout: bool = False
    optional_reason: str | None = None


def as_optional_str(value: object) -> str | None:
    if not value:
        return None
    return str(value)


def as_optional_int(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value


def _rerank_row(
    candidate: RetrievalCandidate, info: Mapping[str, Any]
) -> dict[str, Any]:
    summary = str(info.get("summary") or "")
    return {
        "candidate_id": candidate.candidate_id,
        "path": candidate.relative_path,
        "relative_path": candidate.relative_path,
        "summary": summary,
        "content": str(info.get("content") or summary or ""),
        "title": str(info.get("title") or Path(candidate.relative_path).stem),
        "rrf_score": candidate.rrf_score,
        "score": candidate.rrf_score,
        "bm25_rank": candidate.bm25_rank,
        "vector_rank": candidate.vector_rank,
        "bm25_score": candidate.bm25_score,
        "vector_score": candidate.vector_score,
        "graph_rank": candidate.graph_rank,
        "graph_score": candidate.graph_score,
        "source_sha256": candidate.source_sha256,
        "heading_path": candidate.heading_path,
        "parent_id": candidate.parent_id,
        "byte_start": candidate.byte_start,
        "byte_end": candidate.byte_end,
        "evidence_ids": candidate.evidence_ids,
        "authority": info.get("authority"),
        "authority_weight": candidate.authority_weight,
        "type_weight": candidate.type_weight,
    }


def _rerank_rows(
    candidates: Sequence[RetrievalCandidate],
    display_meta: Mapping[str, Mapping[str, Any]],
    query_norm: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Rows the reranker reads, and whether one title matches the query exactly."""
    rows = [
        _rerank_row(candidate, display_meta.get(candidate.candidate_id, {}))
        for candidate in candidates
    ]
    exact_title_hit = any(
        str(row["title"]).casefold().strip() == query_norm for row in rows
    )
    return rows, exact_title_hit


def _promote_exact_title(
    candidates: Sequence[RetrievalCandidate],
    rows: list[dict[str, Any]],
    query_norm: str,
) -> tuple[RetrievalCandidate, ...]:
    """A title equal to the query goes first, before any reranking."""
    rows.sort(key=lambda row: _exact_title_order(row, query_norm))
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    promoted = [
        by_id[str(row["candidate_id"])]
        for row in rows
        if str(row["candidate_id"]) in by_id
    ]
    return tuple(promoted) or tuple(candidates)


def _exact_title_order(row: Mapping[str, Any], query_norm: str) -> tuple[Any, ...]:
    exact = str(row.get("title", "")).casefold().strip() == query_norm
    return (0 if exact else 1, -float(row.get("rrf_score") or 0.0), str(row.get("candidate_id") or ""))


def candidate_from_rerank_row(row: Mapping[str, Any]) -> RetrievalCandidate:
    path = str(first_present(row, ("relative_path", "path"), ""))
    return RetrievalCandidate(
        candidate_id=str(first_present(row, ("candidate_id", "path"), "")),
        parent_id=str(first_present(row, ("parent_id", "path"), "")),
        relative_path=path,
        heading_path=heading_path_of(row.get("heading_path")),
        source_sha256=source_sha256(row),
        byte_start=as_int(row.get("byte_start"), 0),
        byte_end=as_int(row.get("byte_end"), 0),
        bm25_rank=as_optional_int(row.get("bm25_rank")),
        bm25_score=as_float(row.get("bm25_score")),
        vector_rank=as_optional_int(row.get("vector_rank")),
        vector_score=as_float(row.get("vector_score")),
        graph_rank=as_optional_int(row.get("graph_rank")),
        graph_score=as_float(row.get("graph_score")),
        rrf_score=float(first_present(row, ("rrf_score",), 0.0)),
        rerank_score=as_float(row.get("rerank_score")),
        authority_weight=float(first_present(row, ("authority_weight",), 1.0)),
        type_weight=float(first_present(row, ("type_weight",), 1.0)),
        final_score=float(first_present(row, ("final_score", "rrf_score"), 0.0)),
        evidence_ids=evidence_ids_of(row),
    )


def _rerank_pool_limit(limit: int, max_candidates: int | None) -> int:
    pool_limit = max(limit, 20) if limit > 0 else 20
    if max_candidates is not None and int(max_candidates) > 0:
        return min(pool_limit, int(max_candidates))
    return pool_limit


def _record_reranked(
    reranked: Sequence[Mapping[str, Any]], trace: RerankTrace
) -> None:
    head = reranked[0]
    trace.model_id = head.get("reranker_model_id")
    trace.model_revision = head.get("reranker_model_revision")
    trace.depth = head.get("reranker_depth")
    trace.duration_ms = head.get("reranker_duration_ms")
    if head.get("reranker_applied"):
        trace.applied = True
        trace.fallback_reason = None
        return
    trace.fallback_reason = str(
        head.get("reranker_fallback_reason") or "reranker_unavailable"
    )


def run_reranker(
    rows: list[dict[str, Any]],
    *,
    query: str,
    pool_limit: int,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
    source_stage: str | None = None,
) -> Sequence[Mapping[str, Any]]:
    require_optional_not_cancelled(cancelled)
    from reranker import rerank as _rerank

    def call(deadline: float | None = None) -> Sequence[Mapping[str, Any]]:
        return _rerank(
            query,
            rows[:pool_limit],
            limit=pool_limit,
            text_field="content",
            deadline=deadline,
            cancelled=cancelled,
        )

    if deadline_monotonic is None:
        return call()
    # The same instant the bound abandons this stage at, handed to the stage
    # itself. Abandoning only stops the caller waiting: the thread keeps
    # scoring, on the same four cores as the answer that is now being built
    # without it. Told when to stop, it stops between batches instead.
    require_optional_deadline(deadline_monotonic)
    stage_deadline = source_rerank_deadline(deadline_monotonic, source_stage)
    kind = source_stage or "rerank"
    worker_deadline = rerank_worker_deadline(stage_deadline, kind=kind)
    return run_optional_bounded(
        lambda: call(worker_deadline),
        deadline=stage_deadline,
        cancelled=cancelled,
        kind=kind,
        observes=rerank_scored,
    )


def source_rerank_deadline(deadline: float, source_stage: str | None) -> float:
    """Sequential source passes spend the remaining budget, keeping the measured tail.

    The old half-share twice refused warm event scoring under the normal MCP
    deadline. The paired experiment keeps that deadline, stage ceiling and
    mandatory tail reserve; it introduces no longer request or extra thread.
    See docs/research/2026-09-29-breadcrumb-durability-proposal.md.
    """
    if source_stage is None:
        return optional_stage_deadline(deadline)
    return min(deadline - OPTIONAL_STAGE_TAIL_RESERVE_SECONDS,
               time.monotonic() + retrieval_optional_stages.OPTIONAL_STAGE_MAX_SECONDS)


def rerank_worker_deadline(stage_deadline: float, *, kind: str = "rerank") -> float:
    """When the rerank itself stops: the window, once to learn its cost, or never started.

    A straggler told to stop at the caller's window never finished under load,
    so it never recorded a cost and was started and cut on every call (audit
    B-17, docs/research/2026-09-25-a-rerank-that-cannot-fit-is-not-started.md).
    """
    if optional_stage_fits(kind, stage_deadline):
        return stage_deadline
    if _rerank_cost_worth_learning(kind):
        return time.monotonic() + retrieval_optional_stages.OPTIONAL_STAGE_MAX_SECONDS
    raise OptionalStageNotAdmitted("the rerank is known not to fit this window")


def _rerank_cost_worth_learning(kind: str = "rerank") -> bool:
    """No cost yet, or one old enough that the model may be warm again."""
    if observed_optional_stage_cost(kind) is None:
        return True
    return observed_cost_is_stale(kind)


def rerank_scored(reranked: Sequence[Mapping[str, Any]]) -> bool:
    """Whether the stage really scored, or returned the fused order it was given.

    A rerank cut by its own deadline (and an unavailable or failed one) returns
    normally with every document marked not applied. Timing such a run teaches
    the admission model a cost bounded by the budget the stage was given, so a
    warm reranker that needs longer than the share on offer would be admitted,
    waited for and abandoned on every call.
    """
    if not reranked:
        return False
    return bool(reranked[0].get("reranker_applied"))


def reranked_candidates(
    candidates: Sequence[RetrievalCandidate],
    rows: list[dict[str, Any]],
    *,
    analysis: QueryAnalysis,
    requested: str,
    limit: int,
    max_candidates: int | None,
    rerank_enabled: bool,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
    trace: RerankTrace,
) -> tuple[RetrievalCandidate, ...]:
    from reranker import should_rerank

    apply, skip_reason = should_rerank(
        profile=requested,
        candidates=rows,
        analysis_intents=analysis.intents,
        rerank_enabled=rerank_enabled,
    )
    if not apply:
        trace.fallback_reason = skip_reason
        return tuple(candidates)
    options = dict(
        query=analysis.normalized_query or analysis.query,
        pool_limit=_rerank_pool_limit(limit, max_candidates),
        deadline_monotonic=deadline_monotonic,
        cancelled=cancelled,
    )
    return _rerank_admitted_sources(candidates, rows, trace, options)


def _rerank_admitted_sources(candidates, rows, trace, options):
    primary, events = _rerank_source_groups(candidates)
    if primary and events:
        return _rerank_breadcrumb_groups(candidates, rows, primary, events, trace, options)
    ordered = primary or events
    reranked = run_reranker(_aligned_rerank_rows(ordered, rows), **options)
    check_stopped(options["deadline_monotonic"], options["cancelled"])
    return _candidates_after_rerank(candidates, reranked, trace)


def breadcrumb_candidate(candidate: RetrievalCandidate) -> bool:
    """The corpus admits only verified linked evidence under this private root."""
    return candidate.relative_path.startswith("knowledge/raw/sessions/")


def _rerank_source_groups(candidates):
    primary = _source_candidates(candidates, events=False)
    events = _source_candidates(candidates, events=True)
    # Trust remains in the final score. It must not hide the semantic match
    # before the model can see it (the reproduced cross-language failure).
    primary.sort(key=_semantic_admission_order)
    events.sort(key=_semantic_admission_order)
    return primary, events


def _semantic_admission_order(candidate: RetrievalCandidate) -> float:
    """Semantic relevance admits; provenance still weighs the model's final score."""
    return -(candidate.vector_score or 0.0)


def _aligned_rerank_rows(candidates, rows):
    by_id = {row["candidate_id"]: row for row in rows}
    return [by_id[item.candidate_id] for item in candidates]


def _source_candidates(candidates, *, events: bool):
    return [item for item in candidates if breadcrumb_candidate(item) == events]


def _rerank_breadcrumb_groups(candidates, rows, primary, events, trace, options):
    by_id = {row["candidate_id"]: row for row in rows}
    base = _rerank_source_group(primary, by_id, "rerank", trace, options)
    if not trace.applied:
        return tuple(candidates)
    secondary = RerankTrace()
    supporting = _rerank_source_group(events, by_id, "rerank_events", secondary, options)
    _merge_source_rerank_trace(trace, secondary)
    return _merge_source_reranks(candidates, base, supporting)


def _rerank_source_group(candidates, by_id, kind, trace, options):
    rows = [by_id[item.candidate_id] for item in candidates]
    try:
        reranked = run_reranker(rows, source_stage=kind, **options)
        check_stopped(options["deadline_monotonic"], options["cancelled"])
        return _candidates_after_rerank(candidates, reranked, trace)
    except OptionalStageTimeout as stopped:
        trace.fallback_reason = stopped.reason
        trace.optional_timeout = stopped.partial
        trace.optional_reason = stopped.reason
    except TimeoutError:
        raise
    except Exception as exc:  # noqa: BLE001 - retain the completed primary pass
        note_generation_degradation("reranker", exc)
        trace.fallback_reason = "reranker_error"
    return tuple(candidates)


def _merge_source_rerank_trace(primary: RerankTrace, secondary: RerankTrace) -> None:
    _add_source_rerank_measurements(primary, secondary)
    primary.fallback_reason = secondary.fallback_reason or primary.fallback_reason
    primary.optional_timeout = secondary.optional_timeout or primary.optional_timeout
    primary.optional_reason = secondary.optional_reason or primary.optional_reason


def _add_source_rerank_measurements(primary: RerankTrace, secondary: RerankTrace) -> None:
    primary.depth = (primary.depth or 0) + (secondary.depth or 0)
    primary.duration_ms = (primary.duration_ms or 0) + (secondary.duration_ms or 0)


def _merge_source_reranks(original, primary, events):
    # The same cross-encoder, sigmoid, fusion blend and provenance weights
    # produce comparable scores: no per-pool normalization or output quota.
    scored = [item for item in (*primary, *events) if item.rerank_score is not None]
    scored.sort(key=lambda item: (-item.final_score, item.candidate_id))
    return (*scored, *_below_rerank_pool(original, scored))


def _candidates_after_rerank(
    candidates: Sequence[RetrievalCandidate],
    reranked: Sequence[Mapping[str, Any]],
    trace: RerankTrace,
) -> tuple[RetrievalCandidate, ...]:
    """The reranked order, then the pool the reranker never saw."""
    if not reranked:
        return tuple(candidates)
    _record_reranked(reranked, trace)
    if not trace.applied:
        return tuple(candidates)
    scored = tuple(candidate_from_rerank_row(row) for row in reranked)
    return scored + _below_rerank_pool(candidates, scored)


def _below_rerank_pool(
    candidates: Sequence[RetrievalCandidate], scored: Sequence[RetrievalCandidate]
) -> tuple[RetrievalCandidate, ...]:
    """Candidates below the reranker's bounded pool, in their fused order.

    The reranker reads a bounded prefix, so dropping the rest here decides the
    answer before the page-diverse order runs: measured on this vault, a query
    reached that order with twenty candidates drawn from four pages, and the
    last two visible slots could only repeat a page. Keeping the tail costs
    nothing — it stays behind every reranked row, and the final cut is the same
    one it always was.
    """
    scored_ids = {item.candidate_id for item in scored}
    return tuple(item for item in candidates if item.candidate_id not in scored_ids)


def apply_reranking(
    candidates: Sequence[RetrievalCandidate],
    display_meta: Mapping[str, Mapping[str, Any]],
    *,
    analysis: QueryAnalysis,
    requested: str,
    limit: int,
    max_candidates: int | None,
    rerank_enabled: bool,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
    trace: RerankTrace,
) -> tuple[RetrievalCandidate, ...]:
    """Reorder by cross-encoder when it is worth it; degrade without failing."""
    query_norm = (analysis.normalized_query or analysis.query).casefold().strip()
    try:
        return rerank_or_promote(
            candidates,
            display_meta,
            query_norm,
            analysis=analysis,
            requested=requested,
            limit=limit,
            max_candidates=max_candidates,
            rerank_enabled=rerank_enabled,
            deadline_monotonic=deadline_monotonic,
            cancelled=cancelled,
            trace=trace,
        )
    except OptionalStageTimeout as stopped:
        trace.fallback_reason = stopped.reason
        trace.optional_timeout = stopped.partial
        trace.optional_reason = stopped.reason
    except TimeoutError:
        raise
    except Exception as exc:  # noqa: BLE001 - a failed reranker keeps the fused order
        note_generation_degradation("reranker", exc)
        trace.fallback_reason = "reranker_error"
    return tuple(candidates)


def rerank_or_promote(
    candidates: Sequence[RetrievalCandidate],
    display_meta: Mapping[str, Mapping[str, Any]],
    query_norm: str,
    *,
    analysis: QueryAnalysis,
    requested: str,
    limit: int,
    max_candidates: int | None,
    rerank_enabled: bool,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
    trace: RerankTrace,
) -> tuple[RetrievalCandidate, ...]:
    rows, exact_title_hit = _rerank_rows(candidates, display_meta, query_norm)
    if exact_title_hit:
        trace.fallback_reason = "exact_title_bypass"
        return _promote_exact_title(candidates, rows, query_norm)
    return reranked_candidates(
            candidates,
            rows,
            analysis=analysis,
            requested=requested,
            limit=limit,
            max_candidates=max_candidates,
            rerank_enabled=rerank_enabled,
            deadline_monotonic=deadline_monotonic,
            cancelled=cancelled,
            trace=trace,
        )
