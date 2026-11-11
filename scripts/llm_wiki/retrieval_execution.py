"""The retrieval plan: fusion, optional reranking, partial answers and retrieve."""
from __future__ import annotations

from collections.abc import (
    Callable,
    Mapping,
    Sequence,
)
from dataclasses import (
    dataclass,
)
from dataclasses import (
    field as dataclass_field,
)
from typing import Any

from llm_wiki import retrieval_fusion, retrieval_query
from llm_wiki.retrieval_fusion import (
    resolve_effective_mode,
)
from llm_wiki.retrieval_generation_hits import (
    any_true,
    candidate_backend_limit,
    first_reason,
    query_text_of,
    requested_or_recommended,
)
from llm_wiki.retrieval_query import (
    GRAPH_GLOBAL_LIMIT,
    GRAPH_PER_SEED_LIMIT,
    PROFILE_SIGNALS,
    BackendFn,
    QueryAnalysis,
    RetrievalCandidate,
    RetrievalResult,
    RetrievalStopped,
    RetrievalTrace,
    check_stopped,
    promote_exact_filename,
)
from llm_wiki.retrieval_rerank import (
    RerankTrace,
    apply_reranking,
    as_optional_int,
    as_optional_str,
)
from llm_wiki.retrieval_selection import (
    require_bounded_int,
    visible_order,
)
from llm_wiki.retrieval_stages import (
    BackendRun,
    fusion_input,
    require_known_edge_families,
    run_backends,
)


def fused_candidates(
    backends: BackendRun,
    signals: Sequence[str],
    intents: Sequence[str] | None = None,
    query: str | None = None,
) -> tuple[tuple[RetrievalCandidate, ...], dict[str, dict[str, Any]]]:
    return retrieval_fusion.fuse_rrf(
        lexical=fusion_input(backends.lexical_hits, "lexical", signals),
        dense=fusion_input(backends.dense_hits, "dense", signals),
        graph=fusion_input(backends.graph_hits, "graph", signals),
        intents=intents,
        query=query,
    )


def _capped(
    candidates: Sequence[RetrievalCandidate], cap: int | None
) -> tuple[RetrievalCandidate, ...]:
    """A non-positive or absent cap means every candidate stays."""
    if cap is None or int(cap) <= 0:
        return tuple(candidates)
    return tuple(candidates[: int(cap)])


def _rerank_signals(trace: RerankTrace) -> tuple[str, ...]:
    if trace.applied:
        return ("reranker",)
    return ()


def rerank_failure(trace: RerankTrace, current: str | None) -> str | None:
    return current or trace.optional_reason


def maybe_rerank(
    candidates: tuple[RetrievalCandidate, ...],
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
    """Rerank only when there is something to rerank and it is switched on."""
    if not candidates or not rerank_enabled:
        return candidates
    check_stopped(deadline_monotonic, cancelled)
    return apply_reranking(
        candidates,
        display_meta,
        analysis=analysis,
        requested=requested,
        limit=limit,
        max_candidates=max_candidates,
        rerank_enabled=rerank_enabled,
        deadline_monotonic=deadline_monotonic,
        cancelled=cancelled,
        trace=trace,
    )


def _retrieval_trace(
    *,
    requested: str,
    effective: str,
    signals: Sequence[str],
    fallback: str | None,
    corpus_generation: str,
    partial: bool,
    rerank_trace: RerankTrace,
    wanted: Sequence[str] = (),
) -> RetrievalTrace:
    return RetrievalTrace(
        requested_mode=requested,
        effective_mode=effective,
        signals_used=tuple(dict.fromkeys(signals)),
        fallback_reason=fallback,
        corpus_generation=corpus_generation,
        partial=partial,
        reranker_applied=rerank_trace.applied,
        reranker_model_id=as_optional_str(rerank_trace.model_id),
        reranker_model_revision=as_optional_str(rerank_trace.model_revision),
        reranker_depth=as_optional_int(rerank_trace.depth),
        reranker_duration_ms=as_optional_int(rerank_trace.duration_ms),
        reranker_fallback_reason=rerank_trace.fallback_reason,
        signals_requested=tuple(wanted),
    )


@dataclass
class _PlanProgress:
    """What the plan has finished, carried so an expiry need not discard it.

    `retrieve()` runs its legs in sequence and checks the caller's stop flag
    between them. Before this existed, that check raised and every finished leg
    went in the bin. Measured on this vault under load 13-17: 4.4-5.5 s of a
    10 s budget went to optional stages that returned nothing, and then the
    lexical answer that was already computed was discarded -- 18 of 36 calls
    raised rather than answering. The fields below are filled as work
    completes, so the stop path has something truthful to hand back.
    """

    analysis: QueryAnalysis
    requested: str
    wanted: Sequence[str]
    limit: int
    corpus_generation: str
    graph_enabled: bool
    backends: BackendRun = dataclass_field(default_factory=BackendRun)
    rerank_trace: RerankTrace = dataclass_field(default_factory=RerankTrace)
    candidates: tuple[RetrievalCandidate, ...] | None = None
    display_meta: dict[str, dict[str, Any]] | None = None


def _any_leg_finished(backends: BackendRun) -> bool:
    return backends.ran_lexical or backends.ran_dense or backends.ran_graph


def _partial_candidates(
    progress: _PlanProgress, signals: Sequence[str]
) -> tuple[tuple[RetrievalCandidate, ...], dict[str, dict[str, Any]]]:
    """Fusion already done is reused; otherwise the rows in hand are fused now."""
    if progress.candidates is not None:
        return progress.candidates, progress.display_meta or {}
    return fused_candidates(
        progress.backends, signals, progress.analysis.intents, progress.analysis.query
    )


def _assembled_partial(progress: _PlanProgress, reason: str) -> RetrievalResult:
    """The legs that finished, ranked and labelled. No new work, no new budget.

    Everything here is in-memory work over rows already paid for: fusion,
    exact-filename promotion, page diversity, the cap. No backend is called, the
    reranker is not run, and no stop check is made -- the stop already happened
    and this is the unwinding, not more retrieval.

    The stop reason leads `fallback_reason` because it is the dominant fact: the
    answer is short because the clock ran out. Which legs are missing is not
    lost with it -- `effective_mode` and `signals_used` name exactly the ones
    that finished.
    """
    backends = progress.backends
    effective, fallback, signals = resolve_effective_mode(
        progress.requested,
        wanted=progress.wanted,
        ran_lexical=backends.ran_lexical,
        ran_dense=backends.ran_dense,
        ran_graph=backends.ran_graph,
        dense_available=backends.dense_available,
        graph_available=backends.graph_available,
        graph_enabled=progress.graph_enabled,
    )
    candidates, display_meta = _partial_candidates(progress, signals)
    exact_query = query_text_of(progress.analysis)
    candidates = promote_exact_filename(candidates, exact_query)
    visible = visible_order(
        candidates, display_meta, exact_query, progress.analysis.query, progress.limit
    )
    return RetrievalResult(
        candidates=_capped(visible, progress.limit),
        trace=_retrieval_trace(
            requested=progress.requested,
            effective=effective,
            signals=signals,
            fallback=first_reason(reason, fallback),
            corpus_generation=progress.corpus_generation,
            partial=True,
            rerank_trace=progress.rerank_trace,
            wanted=progress.wanted,
        ),
        analysis=progress.analysis,
        display_meta=display_meta,
    )


def _partial_or_reraise(
    progress: _PlanProgress, stopped: RetrievalStopped
) -> RetrievalResult:
    """Hand back what finished; when nothing did, the stop is the only truth.

    A result with no rows and no signals would be a refusal wearing the clothes
    of an answer, and this vault has ruled against that shape before.
    """
    if not _any_leg_finished(progress.backends):
        raise stopped
    return _assembled_partial(progress, stopped.reason)


def _executed_plan(
    progress: _PlanProgress,
    filters: Mapping[str, Any],
    *,
    lexical_backend: BackendFn | None,
    dense_backend: BackendFn | None,
    graph_backend: BackendFn | None,
    graph_edge_families: Mapping[str, bool] | None,
    graph_per_seed_limit: int,
    graph_global_limit: int,
    rerank_enabled: bool,
    partial: bool,
    max_candidates: int | None,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
) -> RetrievalResult:
    """The plan itself. Every stop inside it leaves `progress` truthful."""
    backends = run_backends(
        progress.backends,
        filters,
        analysis=progress.analysis,
        requested=progress.requested,
        wanted=progress.wanted,
        lexical_backend=lexical_backend,
        dense_backend=dense_backend,
        graph_backend=graph_backend,
        graph_enabled=progress.graph_enabled,
        graph_edge_families=graph_edge_families,
        graph_per_seed_limit=graph_per_seed_limit,
        graph_global_limit=graph_global_limit,
        corpus_generation=progress.corpus_generation,
        deadline_monotonic=deadline_monotonic,
        cancelled=cancelled,
    )
    optional_failure = backends.optional_failure
    partial = any_true(partial, backends.partial)

    effective, fallback, signals = resolve_effective_mode(
        progress.requested,
        wanted=progress.wanted,
        ran_lexical=backends.ran_lexical,
        ran_dense=backends.ran_dense,
        ran_graph=backends.ran_graph,
        dense_available=backends.dense_available,
        graph_available=backends.graph_available,
        graph_enabled=progress.graph_enabled,
    )
    fallback = first_reason(backends.graph_failure, fallback)

    candidates, display_meta = fused_candidates(
        backends, signals, progress.analysis.intents, progress.analysis.query
    )
    exact_query = query_text_of(progress.analysis)
    candidates = promote_exact_filename(candidates, exact_query)
    # Capped before the check, not after, so that what `progress` holds is
    # exactly what the plan holds: a salvage must not answer from a wider pool
    # than the run itself was working with. `_capped` is a pure slice and makes
    # no stop check, so moving it earlier changes nothing else.
    candidates = _capped(candidates, max_candidates)
    progress.candidates = candidates
    progress.display_meta = display_meta
    check_stopped(deadline_monotonic, cancelled)

    rerank_trace = progress.rerank_trace
    candidates = maybe_rerank(
        candidates,
        display_meta,
        analysis=progress.analysis,
        requested=progress.requested,
        limit=progress.limit,
        max_candidates=max_candidates,
        rerank_enabled=rerank_enabled,
        deadline_monotonic=deadline_monotonic,
        cancelled=cancelled,
        trace=rerank_trace,
    )
    signal_list = [*signals, *_rerank_signals(rerank_trace)]
    optional_failure = rerank_failure(rerank_trace, optional_failure)
    partial = any_true(partial, rerank_trace.optional_timeout)

    candidates = promote_exact_filename(candidates, exact_query)
    progress.candidates = candidates
    check_stopped(deadline_monotonic, cancelled)
    visible = visible_order(
        candidates, display_meta, exact_query, progress.analysis.query, progress.limit
    )
    candidates = _capped(visible, progress.limit)

    return RetrievalResult(
        candidates=candidates,
        trace=_retrieval_trace(
            requested=progress.requested,
            effective=effective,
            signals=signal_list,
            fallback=first_reason(optional_failure, fallback),
            corpus_generation=progress.corpus_generation,
            partial=partial,
            rerank_trace=rerank_trace,
            wanted=progress.wanted,
        ),
        analysis=progress.analysis,
        display_meta=display_meta,
    )


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
    signals: Sequence[str] | None = None,
) -> RetrievalResult:
    """Plan and execute retrieval with truthful mode/signal reporting.

    `signals`, when given, are the signals the caller planned for the profile;
    by default the profile's declared ones.

    Lexical and dense backends are invoked independently with identical hard
    filters. Fusion is rank-only RRF. Raw backend scores stay on candidates.

    On expiry the finished legs are returned rather than discarded, labelled
    `partial` with the stop as `fallback_reason`. No new work starts after the
    deadline, so the deadline still binds every backend call; what changes is
    only what happens to work already paid for. When nothing finished there is
    nothing to hand back and the stop propagates as before. See
    `docs/research/2026-08-29-what-an-expired-retrieval-still-owes.md`.
    """
    check_stopped(deadline_monotonic, cancelled)
    analysis = retrieval_query.analyze_query(query)
    requested = requested_or_recommended(requested_profile, analysis)
    backend_limit = candidate_backend_limit(limit, max_candidates)
    filters = {
        "query": analysis.normalized_query or analysis.query,
        "scope": scope,
        "limit": backend_limit,
        "project": project,
        "since": since,
        "as_of": as_of,
    }

    wanted = _planned_signals(requested, signals)
    require_bounded_int(graph_per_seed_limit, 1, 100, "graph_per_seed_limit")
    require_bounded_int(graph_global_limit, 1, 1000, "graph_global_limit")
    require_known_edge_families(graph_edge_families)
    progress = _PlanProgress(
        analysis=analysis,
        requested=requested,
        wanted=wanted,
        limit=limit,
        corpus_generation=corpus_generation,
        graph_enabled=graph_enabled,
    )
    try:
        return _executed_plan(
            progress,
            filters,
            lexical_backend=lexical_backend,
            dense_backend=dense_backend,
            graph_backend=graph_backend,
            graph_edge_families=graph_edge_families,
            graph_per_seed_limit=graph_per_seed_limit,
            graph_global_limit=graph_global_limit,
            rerank_enabled=rerank_enabled,
            partial=partial,
            max_candidates=max_candidates,
            deadline_monotonic=deadline_monotonic,
            cancelled=cancelled,
        )
    except RetrievalStopped as stopped:
        return _partial_or_reraise(progress, stopped)


def _planned_signals(requested: str, signals: Sequence[str] | None) -> tuple[str, ...]:
    if signals is None:
        return PROFILE_SIGNALS[requested]
    planned = tuple(signals)
    if not planned or not set(planned) <= {"lexical", "dense", "graph"}:
        raise ValueError(f"unknown retrieval signals: {planned!r}")
    return planned
