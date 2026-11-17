"""Hits read from the active generation, impressions and reported fallbacks."""
from __future__ import annotations

from collections.abc import (
    Callable,
    Mapping,
    Sequence,
)
from dataclasses import replace
from typing import Any

from llm_wiki import retrieval_graph
from llm_wiki.retrieval_graph import (
    name_dropped,
)
from llm_wiki.retrieval_legacy import backend_hit_from_legacy
from llm_wiki.retrieval_query import (
    CANDIDATE_FANOUT,
    MAX_CANDIDATE_POOL,
    MIN_CANDIDATE_POOL,
    BackendFn,
    GenerationSealChanged,
    QueryAnalysis,
    RetrievalResult,
    RetrievalTrace,
    first_present,
    normalize_profile,
    normalized_filename_stem,
    note_generation_degradation,
)
from llm_wiki.retrieval_stages import (
    filtered_hits,
    require_unchanged_generation,
)

_GENERATION_DENSE_BLOCKING_FALLBACKS = frozenset(
    {"generation_seal_changed", "generation_corrupt", "generation_unavailable"}
)


def generation_seal_holds(
    catalog: Any, context: Mapping[str, Any], stop: Mapping[str, Any]
) -> bool:
    import generation_search

    return bool(
        generation_search.generation_consumption_unchanged(
            catalog,
            context["manifest"],
            context["artifact_names"],
            context["seal"],
            **stop,
        )
    )


def generation_dense_unusable(
    generation_fallback: str | None,
    *,
    embedder: object,
    model_id: object,
    model_revision: object,
) -> str | None:
    """Why the generation's vectors cannot be read, or None when they can."""
    if generation_fallback in _GENERATION_DENSE_BLOCKING_FALLBACKS:
        return generation_fallback
    if embedder is None or model_id is None or model_revision is None:
        return "generation_vectors_unavailable"
    return None


def dense_filtered_hits(
    rows: Sequence[Mapping[str, Any]], filters: Mapping[str, Any]
) -> Sequence[Mapping[str, Any]]:
    """Vector rows carry their distance in `score`; fusion reads vector_score."""
    return filtered_hits(
        [{**row, "vector_score": row.get("score")} for row in rows], filters
    )


def _require_seal(catalog: Any, context: Mapping[str, Any], stop: Mapping[str, Any]) -> None:
    if not generation_seal_holds(catalog, context, stop):
        raise GenerationSealChanged


def generation_graph_hits(
    active_graph: Any,
    filters: Mapping[str, Any],
    *,
    catalog: Any,
    context: Mapping[str, Any],
    stop: Mapping[str, Any],
    cancelled: Callable[[], bool] | None,
) -> Sequence[Mapping[str, Any]]:
    """One hop out of the active generation, sealed before and after the read."""
    _require_seal(catalog, context, stop)
    rows = retrieval_graph.expand_evidence_graph(
        active_graph,
        seeds=filters["seeds"],
        directions=filters["directions"],
        edge_types=filters["edge_types"],
        per_seed_limit=filters["per_seed_limit"],
        global_limit=filters["global_limit"],
        deadline_monotonic=filters["deadline_monotonic"],
        cancelled=cancelled,
    )
    _require_seal(catalog, context, stop)
    return rows


def neighbour_boost_hits(
    lexical_backend: BackendFn, filters: Mapping[str, Any]
) -> Sequence[Mapping[str, Any]]:
    """Legacy path: neighbours of the lexical hits, scored by their boost."""
    from graph_neighbors import boost_graph_neighbors

    seed_filters = {
        key: filters[key]
        for key in ("query", "scope", "limit", "project", "since", "as_of")
    }
    seeds = list(lexical_backend(**seed_filters))
    boosts = boost_graph_neighbors(
        [{"path": hit["path"], "score": hit.get("score", 0)} for hit in seeds], None
    )
    return [
        backend_hit_from_legacy(
            {
                "path": item["path"],
                "candidate_id": item["path"],
                "score": item.get("graph_boost", 0.0),
                "graph_boost": item.get("graph_boost", 0.0),
            }
        )
        for item in boosts
    ]


def drop_generation_connection(context: dict[str, Any], connection: Any) -> None:
    connection.close()
    context["connection"] = None


def generation_connection_for(
    catalog: Any,
    manifest: Mapping[str, Any],
    seal: object,
    stop: Mapping[str, Any],
) -> Any:
    """No seal means no readable generation, so there is nothing to open."""
    if seal is None:
        return None
    import generation_search

    return generation_search.generation_connection(catalog, manifest, seal=seal, **stop)


def close_generation_handles(context: Mapping[str, Any]) -> None:
    """Close the graph and the database of the generation this call opened."""
    for key in ("graph", "connection"):
        handle = context.get(key)
        if handle is None:
            continue
        try:
            handle.close()
        except Exception:  # noqa: BLE001 - a close failure must not mask the result
            pass


_DENSE_REPLACEABLE_REASONS = frozenset({None, "dense_unavailable"})


def _reported_fallback(
    trace_reason: str | None,
    *,
    dense_fallback: str | None,
    generation_fallback: str | None,
    legacy_fallback: str | None,
) -> str | None:
    """One reason, in the order the operator needs to hear it.

    The Markdown read says `no_active_generation` on every row; when a
    generation existed and was refused, its reason is the one that explains
    the answer, so it speaks first (2026-09-23).
    """
    if legacy_fallback and not generation_fallback:
        return legacy_fallback
    if _dense_reason_wins(trace_reason, dense_fallback):
        return str(dense_fallback)
    return _generation_or_trace_reason(trace_reason, generation_fallback)


def _generation_or_trace_reason(
    trace_reason: str | None, generation_fallback: str | None
) -> str | None:
    """A generation reason speaks only while the trace itself is silent."""
    if generation_fallback and trace_reason is None:
        return generation_fallback
    return trace_reason


def _dense_reason_wins(trace_reason: str | None, dense_fallback: str | None) -> bool:
    if not dense_fallback:
        return False
    return trace_reason in _DENSE_REPLACEABLE_REASONS


def _is_exact_filename_answer(result: RetrievalResult, query: str) -> bool:
    """An exact filename stays the answer after fusion and reranking."""
    if not result.candidates:
        return False
    first = normalized_filename_stem(result.candidates[0].relative_path)
    return first == normalized_filename_stem(query)


def reported_mode(result: RetrievalResult, query: str) -> str:
    if _is_exact_filename_answer(result, query):
        return "EXACT"
    return result.trace.effective_mode


def _trace_unchanged(
    trace: RetrievalTrace, effective_mode: str, fallback_reason: str | None, partial: bool
) -> bool:
    return (
        effective_mode == trace.effective_mode
        and fallback_reason == trace.fallback_reason
        and partial == trace.partial
    )


def with_reported_trace(
    result: RetrievalResult,
    *,
    query: str,
    dense_fallback: str | None,
    generation_fallback: str | None,
    legacy_fallback: str | None,
) -> RetrievalResult:
    trace = result.trace
    effective_mode = reported_mode(result, query)
    fallback_reason = _reported_fallback(
        trace.fallback_reason,
        dense_fallback=dense_fallback,
        generation_fallback=generation_fallback,
        legacy_fallback=legacy_fallback,
    )
    partial = trace.partial or legacy_fallback is not None
    if _trace_unchanged(trace, effective_mode, fallback_reason, partial):
        return result
    return RetrievalResult(
        candidates=result.candidates,
        trace=replace(
            trace,
            effective_mode=effective_mode,
            fallback_reason=fallback_reason,
            partial=partial,
        ),
        analysis=result.analysis,
        display_meta=result.display_meta,
    )


def _impression_candidate_id(item: Mapping[str, Any]) -> str:
    """The page that was shown, named the way every other event names a page.

    One column, one identity: the vault-relative path. A generation-mode row
    recorded its chunk hash here, which no reader of the column joins on, so
    those impressions never reached the page's access count; a bare stem is not
    an identity either, since two projects both hold a `state.md`. See
    `docs/research/2026-09-17-one-page-one-identity-and-one-set-of-windows.md`.
    """
    return str(first_present(item, ("path", "relative_path"), ""))


def record_impressions(
    rows: Sequence[Mapping[str, Any]],
    *,
    query: str,
    corpus_generation: str,
    source_tool: str,
) -> None:
    """Best-effort telemetry: never let it affect the answer."""
    if not rows:
        return
    try:
        _emit_impressions(
            rows, query=query, corpus_generation=corpus_generation, source_tool=source_tool
        )
    except Exception as exc:  # noqa: BLE001 - named, never silent; telemetry is not load-bearing
        name_dropped("retrieval_impression", exc)


def _impression_event(
    item: Mapping[str, Any],
    rank: int,
    *,
    query: str,
    corpus_generation: str,
    source_tool: str,
):
    from retrieval_telemetry import best_effort_make_event

    return best_effort_make_event(
        event_kind="impression",
        query=query,
        retrieval_mode=str(item.get("effective_mode") or "base").lower(),
        candidate_id=_impression_candidate_id(item),
        rank=rank,
        generation=str(item.get("generation") or corpus_generation),
        source_tool=source_tool,
    )


def _emit_impressions(
    rows: Sequence[Mapping[str, Any]],
    *,
    query: str,
    corpus_generation: str,
    source_tool: str,
) -> None:
    from retrieval_telemetry import best_effort_record_events

    events = [
        _impression_event(
            item,
            rank,
            query=query,
            corpus_generation=corpus_generation,
            source_tool=source_tool,
        )
        for rank, item in enumerate(rows, start=1)
    ]
    best_effort_record_events([event for event in events if event is not None])


def generation_dense_hits(
    filters: Mapping[str, Any],
    *,
    catalog: Any,
    context: dict[str, Any],
    stop: Mapping[str, Any],
    require_seal: Callable[[], None],
    own_connection: bool,
    embedder: object,
    model_id: object,
    model_revision: object,
) -> Sequence[Mapping[str, Any]] | None:
    """Vectors from the active generation, sealed before and after the read.

    Under a hard deadline the search gets its own connection so a straggler
    cannot outlive the caller on the shared one.
    """

    owned = None
    try:
        require_seal()
        connection, owned = _dense_connection(
            catalog, context, stop, own_connection
        )
        if connection is None:
            return None
        return _dense_hits_or_none(
            filters,
            catalog=catalog,
            context=context,
            connection=connection,
            embedder=embedder,
            model_id=model_id,
            model_revision=model_revision,
            stop=stop,
            require_seal=require_seal,
        )
    except (GenerationSealChanged, TimeoutError):
        raise
    except Exception as exc:  # noqa: BLE001 - unreadable vectors degrade one signal
        note_generation_degradation("generation_vectors", exc)
        require_seal()
        context["dense_fallback"] = "generation_vectors_unavailable"
        return None
    finally:
        _close_quietly(owned)


def _close_quietly(handle: Any) -> None:
    if handle is not None:
        handle.close()


def _dense_connection(
    catalog: Any,
    context: dict[str, Any],
    stop: Mapping[str, Any],
    own_connection: bool,
) -> tuple[Any, Any]:
    """(connection, owned) — `owned` is the caller's to close; None means unusable."""
    if not own_connection:
        return context["connection"], None
    import generation_search

    owned = generation_search.generation_connection(catalog, context["manifest"], **stop)
    if owned is None:
        context["dense_fallback"] = "generation_vectors_unavailable"
        return None, None
    return owned, owned


def _dense_hits_or_none(
    filters: Mapping[str, Any],
    *,
    catalog: Any,
    context: dict[str, Any],
    connection: Any,
    embedder: object,
    model_id: object,
    model_revision: object,
    stop: Mapping[str, Any],
    require_seal: Callable[[], None],
) -> Sequence[Mapping[str, Any]] | None:
    import generation_search

    rows = generation_search.generation_vectors_search(
        filters["query"],
        catalog,
        context["manifest"],
        connection,
        embedder=embedder,
        model_id=model_id,
        model_revision=model_revision,
        scope=filters["scope"],
        limit=filters["limit"],
        project=filters["project"],
        since=filters["since"],
        as_of=filters["as_of"],
        include_unweighted=True,
        **stop,
    )
    require_seal()
    if rows is None:
        context["dense_fallback"] = "generation_vectors_unavailable"
        return None
    return dense_filtered_hits(rows, filters)


def generation_lexical_hits(
    filters: Mapping[str, Any],
    *,
    catalog: Any,
    context: Mapping[str, Any],
    stop: Mapping[str, Any],
) -> Sequence[Mapping[str, Any]]:
    import generation_search

    require_unchanged_generation(catalog, context, stop)
    rows = generation_search.generation_fts_search(
        filters["query"],
        context["manifest"],
        context["connection"],
        scope=filters["scope"],
        limit=filters["limit"],
        project=filters["project"],
        since=filters["since"],
        as_of=filters["as_of"],
        include_unweighted=True,
        **stop,
    )
    require_unchanged_generation(catalog, context, stop)
    return filtered_hits(rows, filters)


def requested_or_recommended(requested_profile: object, analysis: QueryAnalysis) -> str:
    return normalize_profile(requested_profile) or analysis.recommended_profile


def query_text_of(analysis: QueryAnalysis) -> str:
    return analysis.normalized_query or analysis.query


def any_true(*flags: object) -> bool:
    return any(bool(flag) for flag in flags)


def first_reason(*reasons: object) -> Any:
    """The first reason that says something; None when none of them does."""
    for reason in reasons:
        if reason:
            return reason
    return None


def candidate_pool(limit: int) -> int:
    """How many chunks to ask each backend for, so `limit` pages can exist.

    Several chunks of one page are one answer repeated, not several answers. A
    pool the size of the answer therefore cannot hold `limit` distinct pages:
    measured on this vault, twenty-five chunks carried three to seven pages. The
    pool is what makes the page-diverse order downstream have anything to choose
    from, and it costs one bounded index read per backend.
    """
    if limit <= 0:
        return MIN_CANDIDATE_POOL
    return max(MIN_CANDIDATE_POOL, min(limit * CANDIDATE_FANOUT, MAX_CANDIDATE_POOL))


def candidate_backend_limit(limit: int, max_candidates: int | None) -> int:
    """How many rows each backend may return before fusion trims them."""
    pool = candidate_pool(limit)
    if max_candidates is None or int(max_candidates) <= 0:
        return pool
    return min(pool, int(max_candidates))
