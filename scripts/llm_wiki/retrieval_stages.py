"""The lexical, dense and graph stage runs and their fusion inputs."""
from __future__ import annotations

from collections.abc import (
    Callable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from typing import Any

from llm_wiki.retrieval_backends import (
    run_dense_backend,
    run_graph_backend,
)
from llm_wiki.retrieval_legacy import backend_hit_from_legacy
from llm_wiki.retrieval_query import (
    GRAPH_EDGE_DECAY,
    BackendFn,
    GenerationSealChanged,
    QueryAnalysis,
    check_stopped,
)


def wanted_backend(backend: BackendFn | None, wanted: bool) -> BackendFn | None:
    """Hand `retrieve` only the backends this profile and these switches allow."""
    if wanted:
        return backend
    return None


def fusion_input(
    hits: Sequence[Mapping[str, Any]] | None,
    signal: str,
    signals: Sequence[str],
) -> Sequence[Mapping[str, Any]] | None:
    """Only a backend that actually answered contributes a list to the fusion."""
    if signal not in signals:
        return None
    return hits


def require_known_edge_families(families: Mapping[str, bool] | None) -> None:
    if families is None:
        return
    if not _edge_families_are_known(families):
        raise ValueError("graph_edge_families must map known edge types to booleans")


def _edge_families_are_known(families: object) -> bool:
    """A mapping of known edge types to booleans, and nothing else."""
    if not isinstance(families, Mapping):
        return False
    return not any(
        _is_unknown_edge_family(edge, enabled) for edge, enabled in families.items()
    )


def _is_unknown_edge_family(edge: object, enabled: object) -> bool:
    return edge not in GRAPH_EDGE_DECAY or not isinstance(enabled, bool)


@dataclass
class BackendRun:
    """What each backend produced, and what that means for the trace."""

    lexical_hits: Sequence[Mapping[str, Any]] | None = None
    dense_hits: Sequence[Mapping[str, Any]] | None = None
    graph_hits: Sequence[Mapping[str, Any]] | None = None
    ran_lexical: bool = False
    ran_dense: bool = False
    ran_graph: bool = False
    dense_available: bool | None = None
    graph_available: bool | None = None
    graph_failure: str | None = None
    optional_failure: str | None = None
    partial: bool = False


def _run_lexical_stage(
    run: BackendRun,
    lexical_backend: BackendFn | None,
    filters: Mapping[str, Any],
    *,
    wanted: Sequence[str],
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    if lexical_backend is None or "lexical" not in wanted:
        return
    check_stopped(deadline_monotonic, cancelled)
    run.lexical_hits = lexical_backend(**filters) or ()
    run.ran_lexical = True
    check_stopped(deadline_monotonic, cancelled)


def _run_dense_stage(
    run: BackendRun,
    dense_backend: BackendFn | None,
    filters: Mapping[str, Any],
    *,
    wanted: Sequence[str],
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    if dense_backend is None or "dense" not in wanted:
        return
    check_stopped(deadline_monotonic, cancelled)
    run.dense_hits, run.ran_dense, run.dense_available, missing = run_dense_backend(
        dense_backend,
        filters,
        deadline_monotonic=deadline_monotonic,
        cancelled=cancelled,
    )
    if missing is not None:
        # A dense leg that did not run leaves its candidates out: partial either way.
        run.optional_failure = missing
        run.partial = True
    check_stopped(deadline_monotonic, cancelled)


def _graph_stage_runnable(
    run: BackendRun, graph_enabled: bool, graph_backend: BackendFn | None
) -> bool:
    """A disabled graph records its unavailability; a missing backend is silent."""
    if not graph_enabled:
        run.graph_available = False
        return False
    return graph_backend is not None


def _run_graph_stage(
    run: BackendRun,
    graph_backend: BackendFn | None,
    filters: Mapping[str, Any],
    *,
    analysis: QueryAnalysis,
    requested: str,
    wanted: Sequence[str],
    graph_enabled: bool,
    graph_edge_families: Mapping[str, bool] | None,
    graph_per_seed_limit: int,
    graph_global_limit: int,
    corpus_generation: str,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    if "graph" not in wanted:
        return
    if not _graph_stage_runnable(run, graph_enabled, graph_backend):
        return
    check_stopped(deadline_monotonic, cancelled)
    (
        run.graph_hits,
        run.ran_graph,
        run.graph_available,
        run.graph_failure,
    ) = run_graph_backend(
        graph_backend,
        filters,
        analysis=analysis,
        requested=requested,
        lexical_hits=run.lexical_hits,
        dense_hits=run.dense_hits,
        graph_edge_families=graph_edge_families,
        per_seed_limit=graph_per_seed_limit,
        global_limit=graph_global_limit,
        corpus_generation=corpus_generation,
        deadline_monotonic=deadline_monotonic,
    )
    check_stopped(deadline_monotonic, cancelled)


def run_backends(
    run: BackendRun,
    filters: Mapping[str, Any],
    *,
    analysis: QueryAnalysis,
    requested: str,
    wanted: Sequence[str],
    lexical_backend: BackendFn | None,
    dense_backend: BackendFn | None,
    graph_backend: BackendFn | None,
    graph_enabled: bool,
    graph_edge_families: Mapping[str, bool] | None,
    graph_per_seed_limit: int,
    graph_global_limit: int,
    corpus_generation: str,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
) -> BackendRun:
    """Ask each wanted backend in turn; the graph one reads the earlier hits."""
    _run_lexical_stage(
        run,
        lexical_backend,
        filters,
        wanted=wanted,
        deadline_monotonic=deadline_monotonic,
        cancelled=cancelled,
    )
    _run_dense_stage(
        run,
        dense_backend,
        filters,
        wanted=wanted,
        deadline_monotonic=deadline_monotonic,
        cancelled=cancelled,
    )
    _run_graph_stage(
        run,
        graph_backend,
        filters,
        analysis=analysis,
        requested=requested,
        wanted=wanted,
        graph_enabled=graph_enabled,
        graph_edge_families=graph_edge_families,
        graph_per_seed_limit=graph_per_seed_limit,
        graph_global_limit=graph_global_limit,
        corpus_generation=corpus_generation,
        deadline_monotonic=deadline_monotonic,
        cancelled=cancelled,
    )
    return run


def first_fallback_reason(
    rows: Sequence[Mapping[str, Any]], current: str | None
) -> str | None:
    for row in rows:
        reason = row.get("fallback_reason")
        if reason:
            return str(reason)
    return current


def filtered_hits(
    rows: Sequence[Mapping[str, Any]], filters: Mapping[str, Any]
) -> Sequence[Mapping[str, Any]]:
    """Backend rows as candidate hits, with the caller's hard filters applied."""
    import generation_search

    hits = _fusion_signal_hits(rows)
    return generation_search.apply_hard_filters(
        hits,
        project=filters.get("project"),
        since=filters.get("since"),
        as_of=filters.get("as_of"),
        scope=filters.get("scope", "all"),
    )


def _fusion_signal_hit(row):
    hit = backend_hit_from_legacy(row)
    if "_signal_score" not in row:
        return hit
    hit["score"] = float(row["_signal_score"])
    for field in ("bm25_score", "vector_score"):
        if field in hit:
            hit[field] = hit["score"]
    return hit


def _fusion_signal_order(hit):
    return -float(hit["score"]), str(hit["path"])


def _fusion_signal_hits(rows):
    hits = [_fusion_signal_hit(row) for row in rows]
    if not any("_signal_score" in row for row in rows):
        return hits
    return sorted(hits, key=_fusion_signal_order)


def require_unchanged_generation(
    catalog: Any, context: Mapping[str, Any], stop: Mapping[str, Any]
) -> None:
    """The seal must hold before the read and still hold after it."""
    import generation_search

    if not generation_search.generation_consumption_unchanged(
        catalog,
        context["manifest"],
        context["artifact_names"],
        context["seal"],
        **stop,
    ):
        raise GenerationSealChanged
