"""The dense and graph backend calls."""
from __future__ import annotations

from collections.abc import (
    Callable,
    Mapping,
    Sequence,
)
from typing import Any

from llm_wiki.retrieval_graph import (
    graph_seeds,
    prepare_graph_hits,
)
from llm_wiki.retrieval_optional_stages import (
    OptionalStageTimeout,
    optional_stage_deadline,
    require_optional_deadline,
    require_optional_not_cancelled,
    run_optional_bounded,
)
from llm_wiki.retrieval_query import (
    GRAPH_EDGE_DECAY,
    GRAPH_MAX_HOPS,
    GRAPH_PROFILE_DIRECTIONS,
    GRAPH_PROFILE_EDGE_TYPES,
    BackendFn,
    GenerationSealChanged,
    QueryAnalysis,
    note_generation_degradation,
)


def call_dense(
    dense_backend: BackendFn,
    filters: Mapping[str, Any],
    *,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
) -> Sequence[Mapping[str, Any]] | None:
    require_optional_not_cancelled(cancelled)
    if deadline_monotonic is None:
        return dense_backend(**filters)
    require_optional_deadline(deadline_monotonic)
    return run_optional_bounded(
        lambda: dense_backend(**filters),
        deadline=optional_stage_deadline(deadline_monotonic),
        cancelled=cancelled,
        kind="dense",
    )


def run_dense_backend(
    dense_backend: BackendFn,
    filters: Mapping[str, Any],
    *,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
) -> tuple[Sequence[Mapping[str, Any]] | None, bool, bool | None, str | None]:
    """(hits, ran, available, why the leg is missing or None).

    None hits mean the backend is unavailable; an empty sequence means it
    answered and found nothing. A leg that did not run is named as a timeout or
    as not admitted, never one for the other (audit 2026-09-27 B-9).
    """
    try:
        hits = call_dense(
            dense_backend,
            filters,
            deadline_monotonic=deadline_monotonic,
            cancelled=cancelled,
        )
    except OptionalStageTimeout as stopped:
        return None, False, False, stopped.reason
    return hits, True, hits is not None, None


def _profile_edge_types(
    requested: str, graph_edge_families: Mapping[str, bool] | None
) -> tuple[str, ...]:
    """Edge types this profile reads, minus any family the caller switched off."""
    edge_types = GRAPH_PROFILE_EDGE_TYPES.get(requested, tuple(GRAPH_EDGE_DECAY))
    if not graph_edge_families:
        return tuple(edge_types)
    return tuple(
        edge_type
        for edge_type in edge_types
        if graph_edge_families.get(edge_type, True)
    )


def run_graph_backend(
    graph_backend: BackendFn,
    filters: Mapping[str, Any],
    *,
    analysis: QueryAnalysis,
    requested: str,
    lexical_hits: Sequence[Mapping[str, Any]] | None,
    dense_hits: Sequence[Mapping[str, Any]] | None,
    graph_edge_families: Mapping[str, bool] | None,
    per_seed_limit: int,
    global_limit: int,
    corpus_generation: str,
    deadline_monotonic: float | None,
) -> tuple[Sequence[Mapping[str, Any]] | None, bool, bool | None, str | None]:
    """(hits, ran, available, failure reason).

    A deadline or a changed generation seal is the caller's problem and is
    re-raised; any other backend error degrades the graph signal instead of
    failing the whole retrieval.
    """
    directions = GRAPH_PROFILE_DIRECTIONS.get(requested, ("out",))
    edge_types = _profile_edge_types(requested, graph_edge_families)
    seeds = graph_seeds(lexical_hits, dense_hits)
    # Built before the call, not inside it: a profile naming an edge type this
    # product no longer weighs is our own defect, and reading it as "the graph
    # backend failed" hid one for a day. See
    # `docs/research/2026-09-18-a-profile-asks-only-for-edges-this-product-weighs.md`.
    edge_decay = {edge: GRAPH_EDGE_DECAY[edge] for edge in edge_types}
    try:
        raw_hits = graph_backend(
            **filters,
            seeds=seeds,
            max_hops=GRAPH_MAX_HOPS,
            directions=directions,
            edge_types=edge_types,
            edge_decay=edge_decay,
            per_seed_limit=per_seed_limit,
            global_limit=global_limit,
            deadline_monotonic=deadline_monotonic,
            corpus_generation=corpus_generation,
        )
    except (TimeoutError, GenerationSealChanged):
        raise
    except Exception as exc:  # noqa: BLE001 - a broken graph degrades one signal only
        note_generation_degradation("graph_backend", exc)
        return None, False, False, "graph_error"
    return _prepared_graph_outcome(
        raw_hits,
        analysis=analysis,
        requested=requested,
        seeds=seeds,
        directions=directions,
        edge_types=edge_types,
        per_seed_limit=per_seed_limit,
        global_limit=global_limit,
    )


def _prepared_graph_outcome(
    raw_hits: Sequence[Mapping[str, Any]] | None,
    *,
    analysis: QueryAnalysis,
    requested: str,
    seeds: Sequence[Mapping[str, Any]],
    directions: Sequence[str],
    edge_types: Sequence[str],
    per_seed_limit: int,
    global_limit: int,
) -> tuple[Sequence[Mapping[str, Any]] | None, bool, bool | None, str | None]:
    if raw_hits is None:
        return None, True, False, None
    hits = prepare_graph_hits(
        raw_hits,
        query=analysis.normalized_query or analysis.query,
        requested_profile=requested,
        seeds=seeds,
        directions=directions,
        edge_types=edge_types,
        per_seed_limit=per_seed_limit,
        global_limit=global_limit,
    )
    return hits, True, True, None
