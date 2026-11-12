"""The search_memory bridge: planned requests, generation backends and the search run."""
from __future__ import annotations

from collections.abc import (
    Callable,
    Mapping,
    Sequence,
)
from dataclasses import (
    asdict,
    dataclass,
)
from dataclasses import (
    field as dataclass_field,
)
from pathlib import Path
from typing import Any

from llm_wiki import retrieval_execution, retrieval_query, runtime_roots
from llm_wiki.retrieval_generation_hits import (
    close_generation_handles,
    drop_generation_connection,
    first_reason,
    generation_connection_for,
    generation_dense_hits,
    generation_dense_unusable,
    generation_graph_hits,
    generation_lexical_hits,
    generation_seal_holds,
    neighbour_boost_hits,
    record_impressions,
    with_reported_trace,
)
from llm_wiki.retrieval_legacy import candidates_to_legacy
from llm_wiki.retrieval_query import (
    PROFILE_SIGNALS,
    GenerationSealChanged,
    QueryAnalysis,
    RetrievalResult,
    check_stopped,
    normalize_profile,
    note_generation_degradation,
)
from llm_wiki.retrieval_stages import (
    filtered_hits,
    first_fallback_reason,
    wanted_backend,
)


def planned_request(
    profile: str | None, analysis: QueryAnalysis, *, semantic: bool
) -> tuple[str, tuple[str, ...]]:
    """The profile a search runs and the signals it asks for.

    `semantic=False` forces the lexical profile whatever the planner says, and an
    explicit profile runs as declared. Otherwise a question the planner reads as
    one about relations (a profile that declares the graph) keeps that profile
    and adds dense; every other question runs HYBRID. Before 2026-09-25 every
    semantic search ran HYBRID, so the evidence graph never answered `recall`
    (audit C-22, docs/research/2026-09-25-recall-asks-the-graph-about-relations.md).
    """
    if not semantic:
        return "BASE", ("lexical",)
    requested = normalize_profile(profile)
    if requested is not None:
        return requested, PROFILE_SIGNALS[requested]
    return _planned_semantic_request(analysis.recommended_profile)


def _planned_semantic_request(recommended: str) -> tuple[str, tuple[str, ...]]:
    declared = PROFILE_SIGNALS[recommended]
    if "graph" not in declared:
        return "HYBRID", PROFILE_SIGNALS["HYBRID"]
    return recommended, tuple(dict.fromkeys(("lexical", "dense", *declared)))


def _selected_catalog(catalog: Any) -> Any:
    if catalog is not None:
        return catalog
    import generation_search

    return generation_search.active_generation_catalog()


def _generation_stop(
    deadline_monotonic: float | None, cancelled: Callable[[], bool] | None
) -> dict[str, Any]:
    stop: dict[str, Any] = {}
    if deadline_monotonic is not None:
        stop["deadline"] = deadline_monotonic
    if cancelled is not None:
        stop["cancelled"] = cancelled
    return stop


def _optional_stop(hard_deadline: bool, stop: dict[str, Any]) -> dict[str, Any]:
    """Optional stages get no deadline of their own under a hard one."""
    if hard_deadline:
        return {}
    return stop


def _optional_value(hard_deadline: bool, value: Any) -> Any:
    if hard_deadline:
        return None
    return value


def _catalog_requested(catalog: Any, page_paths: Any) -> bool:
    return catalog is not None and page_paths is None


def _wants_vectors(wanted: Sequence[str], semantic: bool) -> bool:
    return "dense" in wanted and semantic


def _generation_naming(
    use_generation: bool,
    catalog_requested: bool,
    context: Mapping[str, Any],
    corpus_generation: str,
) -> tuple[str, str | None]:
    """(corpus generation, generation fallback) as the trace must report them."""
    if use_generation:
        return str(context["manifest"]["generation_id"]), None
    if catalog_requested:
        # Always continue through retrieve(); report truthful generation failure.
        return corpus_generation, "generation_unavailable"
    return corpus_generation, None


def active_manifest_for(
    catalog: Any,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
    stop: Mapping[str, Any],
) -> tuple[Any, Any] | None:
    import generation_search

    try:
        from repository_scope import resolve_repository_scope

        scope = resolve_repository_scope(
            runtime_roots.vault_root(), deadline=deadline, cancelled=cancelled
        )
        manifest = catalog.get_active_for_repository(scope, **stop)
    except TimeoutError:
        raise
    except Exception as exc:  # noqa: BLE001 - no usable generation is not an error
        generation_search.note_degradation("generation_manifest", exc)
        return None
    if not isinstance(manifest, dict):
        return None
    return scope, manifest


def _note_stale_vectors(
    context: dict[str, Any], manifest: Mapping[str, Any], want_vectors: bool
) -> None:
    if not want_vectors or manifest.get("vector_state") != "stale":
        return
    context["dense_fallback"] = "generation_vectors_unavailable"
    context["legacy_dense_blocked"] = True


def _generation_lexical_or_raise(
    filters: Mapping[str, Any],
    *,
    catalog: Any,
    context: Mapping[str, Any],
    stop: Mapping[str, Any],
    note: Callable[[str], None],
) -> Sequence[Mapping[str, Any]]:
    """Lexical hits from the generation; any failure sends the caller to Markdown."""
    try:
        return generation_lexical_hits(
            filters, catalog=catalog, context=context, stop=stop
        )
    except GenerationSealChanged:
        note("generation_seal_changed")
        raise
    except TimeoutError:
        raise
    except Exception as exc:  # noqa: BLE001 - the label reaches the trace, the cause the log
        note_generation_degradation("generation_lexical", exc)
        note("generation_corrupt")
        raise GenerationSealChanged


def _generation_dense_backend_hits(
    filters: Mapping[str, Any],
    *,
    catalog: Any,
    context: dict[str, Any],
    stop: Mapping[str, Any],
    require_seal: Callable[[], None],
    own_connection: bool,
    generation_fallback: str | None,
    embedder: object,
    model_id: object,
    model_revision: object,
) -> Sequence[Mapping[str, Any]] | None:
    unusable = generation_dense_unusable(
        generation_fallback,
        embedder=embedder,
        model_id=model_id,
        model_revision=model_revision,
    )
    if unusable is not None:
        context["dense_fallback"] = unusable
        return None
    return generation_dense_hits(
        filters,
        catalog=catalog,
        context=context,
        stop=stop,
        require_seal=require_seal,
        own_connection=own_connection,
        embedder=embedder,
        model_id=model_id,
        model_revision=model_revision,
    )


def _generation_graph_backend_hits(
    filters: Mapping[str, Any],
    *,
    catalog: Any,
    context: Mapping[str, Any],
    stop: Mapping[str, Any],
    cancelled: Callable[[], bool] | None,
) -> Sequence[Mapping[str, Any]] | None:
    active_graph = context.get("graph")
    if active_graph is None:
        return None
    return generation_graph_hits(
        active_graph,
        filters,
        catalog=catalog,
        context=context,
        stop=stop,
        cancelled=cancelled,
    )


def _neighbour_boost_or_none(
    lexical_backend: Callable[..., Sequence[Mapping[str, Any]]],
    filters: Mapping[str, Any],
) -> Sequence[Mapping[str, Any]] | None:
    try:
        return neighbour_boost_hits(lexical_backend, filters)
    except TimeoutError:
        raise
    except Exception as exc:  # noqa: BLE001 - the graph signal degrades on its own
        note_generation_degradation("graph_neighbours", exc)
        return None


def resolved_query_encoder(
    *,
    semantic: bool,
    embedder: object | None,
    model_id: str | None,
    model_revision: str | None,
) -> tuple[object | None, str | None, str | None]:
    """Give this entry point the encoder that `search()` resolves for itself.

    The two entry points disagreed about the semantic flag until 2026-08-25 and
    the flag was fixed then; they still disagreed about the encoder, which is
    the other half of the same trap. `search()` resolves one before delegating,
    so a caller that enters here directly — the grounded answer does — left it
    None and the generation reader refused the dense leg as
    `generation_vectors_unavailable`. Measured on this vault: the grounded
    answer to "как устроен повтор после карантина" was built from six lexical
    rows, four of them the same status document, and reported insufficient
    evidence for a page the vault holds.
    """
    if not semantic or embedder is not None:
        return embedder, model_id, model_revision
    import generation_search

    return generation_search.resolved_generation_embedder(True, None, model_id, model_revision)


@dataclass
class _SearchRun:
    """One `retrieve_via_search_memory` call: its arguments and the state its backends share.

    The backends were closures over this state until 2026-09-11 (audit L8,
    docs/research/2026-09-11-one-search-run-is-an-object-not-ten-closures.md).
    """

    query: str
    scope: str
    limit: int
    project: str | None
    since: str | None
    as_of: str | None
    semantic: bool
    page_paths: list[Path] | None
    graph: bool
    rerank: bool
    catalog: Any
    embedder: object | None
    model_id: str | None
    model_revision: str | None
    deadline: float | None
    max_candidates: int | None
    cancelled: Callable[[], bool] | None
    requested: Any
    wanted: tuple[str, ...]
    generation_ctx: dict[str, Any] = dataclass_field(
        default_factory=lambda: {
            "manifest": None,
            "connection": None,
            "graph": None,
            "seal": None,
            "dense_fallback": None,
            "legacy_dense_blocked": False,
        }
    )
    use_generation: bool = False
    corpus_generation: str = "legacy"
    generation_fallback: str | None = None
    legacy_fallback: str | None = None

    def __post_init__(self) -> None:
        self.hard_deadline = self.deadline is not None
        self.generation_stop = _generation_stop(self.deadline, self.cancelled)
        self.optional_generation_stop = _optional_stop(self.hard_deadline, self.generation_stop)
        self.optional_deadline = _optional_value(self.hard_deadline, self.deadline)
        self.optional_cancelled = _optional_value(self.hard_deadline, self.cancelled)

    # --- the generation this run reads ---------------------------------------

    def artifact_names_for(self, manifest: dict[str, object], *, want_vectors: bool) -> tuple[str, ...]:
        import generation_search

        names: list[str] = [generation_search.GENERATION_FTS_ARTIFACT]
        if want_vectors and manifest.get("vector_state") == "complete":
            names.extend(generation_search.GENERATION_VECTOR_ARTIFACTS)
        if "graph" in self.wanted and generation_search.generation_artifact(
            manifest, "evidence.sqlite3"
        ):
            names.append("evidence.sqlite3")
        return tuple(names)

    def resolved_manifest(self, want_vectors: bool) -> tuple[Any, Any] | None:
        """(scope, manifest) for the active generation, or None when unusable."""
        resolved = active_manifest_for(
            self.catalog,
            deadline=self.deadline,
            cancelled=self.cancelled,
            stop=self.generation_stop,
        )
        if resolved is None:
            return None
        _note_stale_vectors(self.generation_ctx, resolved[1], want_vectors)
        return resolved

    def attach_graph(self, repository_scope: Any, connection: Any) -> bool:
        try:
            from evidence_graph import EvidenceGraph

            self.generation_ctx["graph"] = EvidenceGraph.open_active_for_repository(
                self.catalog,
                repository_scope,
                deadline=self.deadline,
                cancelled=self.cancelled,
            )
        except TimeoutError:
            drop_generation_connection(self.generation_ctx, connection)
            raise
        except Exception as exc:  # noqa: BLE001 - an unusable graph drops the generation
            note_generation_degradation("generation_graph", exc)
            drop_generation_connection(self.generation_ctx, connection)
            self.generation_ctx["graph"] = None
            return False
        if self.generation_ctx["graph"] is None:
            drop_generation_connection(self.generation_ctx, connection)
            return False
        return True

    def catalog_requested(self) -> bool:
        return _catalog_requested(self.catalog, self.page_paths)

    def open_generation(self, *, want_vectors: bool) -> bool:
        if not self.catalog_requested():
            return False
        resolved = self.resolved_manifest(want_vectors)
        if resolved is None:
            return False
        return self.adopt_generation(resolved, want_vectors=want_vectors)

    def adopt_generation(self, resolved: Any, *, want_vectors: bool) -> bool:
        """Publish one resolved manifest as the generation this query reads."""
        repository_scope, manifest = resolved
        artifact_names = self.artifact_names_for(manifest, want_vectors=want_vectors)
        import generation_search

        seal = generation_search.generation_consumption_seal(
            self.catalog, manifest, artifact_names, **self.generation_stop
        )
        connection = generation_connection_for(
            self.catalog, manifest, seal, self.generation_stop
        )
        if connection is None:
            return False
        self.generation_ctx["manifest"] = manifest
        self.generation_ctx["connection"] = connection
        self.generation_ctx["seal"] = seal
        self.generation_ctx["artifact_names"] = artifact_names
        if "evidence.sqlite3" not in artifact_names:
            return True
        return self.attach_graph(repository_scope, connection)

    def open(self) -> None:
        """Decide whether this run reads the active generation, and name what it reads."""
        want_vectors = _wants_vectors(self.wanted, self.semantic)
        self.use_generation = self.open_generation(want_vectors=want_vectors)
        self.corpus_generation, self.generation_fallback = _generation_naming(
            self.use_generation,
            self.catalog_requested(),
            self.generation_ctx,
            self.corpus_generation,
        )

    def note_generation_fallback(self, reason: str) -> None:
        self.generation_fallback = self.generation_fallback or reason

    # --- the three backends ---------------------------------------------------

    def lexical_backend(self, **filters: Any) -> Sequence[Mapping[str, Any]]:
        if self.use_generation:
            return _generation_lexical_or_raise(
                filters,
                catalog=self.catalog,
                context=self.generation_ctx,
                stop=self.generation_stop,
                note=self.note_generation_fallback,
            )
        import generation_search

        rows = generation_search.markdown_hits(
            filters["query"],
            scope=filters["scope"],
            limit=filters["limit"],
            project=filters["project"],
            since=filters["since"],
            as_of=filters["as_of"],
            page_paths=self.page_paths,
            deadline=self.deadline,
            cancelled=self.cancelled,
            include_unweighted=True,
        )
        self.legacy_fallback = first_fallback_reason(rows, self.legacy_fallback)
        return filtered_hits(rows, filters)

    def require_seal(self) -> None:
        if generation_seal_holds(self.catalog, self.generation_ctx, self.optional_generation_stop):
            return
        self.generation_ctx["dense_fallback"] = "generation_seal_changed"
        self.generation_fallback = "generation_seal_changed"
        raise GenerationSealChanged

    def dense_backend(self, **filters: Any) -> Sequence[Mapping[str, Any]] | None:
        if "dense" not in self.wanted or self.generation_ctx["legacy_dense_blocked"]:
            return None
        if self.use_generation:
            return _generation_dense_backend_hits(
                filters,
                catalog=self.catalog,
                context=self.generation_ctx,
                stop=self.optional_generation_stop,
                require_seal=self.require_seal,
                own_connection=self.hard_deadline,
                generation_fallback=self.generation_fallback,
                embedder=self.embedder,
                model_id=self.model_id,
                model_revision=self.model_revision,
            )
        return None

    def graph_backend(self, **filters: Any) -> Sequence[Mapping[str, Any]] | None:
        if not self.graph or "graph" not in self.wanted:
            return None
        if self.use_generation:
            return _generation_graph_backend_hits(
                filters,
                catalog=self.catalog,
                context=self.generation_ctx,
                stop=self.generation_stop,
                cancelled=self.cancelled,
            )
        return _neighbour_boost_or_none(self.lexical_backend, filters)

    # --- the run ----------------------------------------------------------------

    def run_retrieval(self) -> RetrievalResult:
        return retrieval_execution.retrieve(
            self.query,
            requested_profile=self.requested,
            scope=self.scope,
            limit=self.limit,
            project=self.project,
            since=self.since,
            as_of=self.as_of,
            lexical_backend=wanted_backend(self.lexical_backend, "lexical" in self.wanted),
            dense_backend=wanted_backend(
                self.dense_backend, "dense" in self.wanted and self.semantic
            ),
            graph_backend=wanted_backend(
                self.graph_backend, "graph" in self.wanted and self.graph
            ),
            corpus_generation=self.corpus_generation,
            graph_enabled=self.graph,
            rerank_enabled=self.rerank,
            partial=False,
            deadline_monotonic=self.deadline,
            max_candidates=self.max_candidates,
            cancelled=self.cancelled,
            signals=self.wanted,
        )

    def run_under_seal(self) -> RetrievalResult:
        """One run, valid only while the generation seal still holds."""
        outcome = self.run_retrieval()
        if self.use_generation and not generation_seal_holds(
            self.catalog, self.generation_ctx, self.generation_stop
        ):
            self.generation_fallback = "generation_seal_changed"
            raise GenerationSealChanged
        return outcome

    def run(self) -> RetrievalResult:
        """Run under the seal; a seal that changed mid-run is answered from legacy."""
        try:
            try:
                return self.run_under_seal()
            except GenerationSealChanged:
                self.use_generation = False
                self.corpus_generation = "legacy"
                return self.run_retrieval()
        finally:
            close_generation_handles(self.generation_ctx)

    def reported(self, result: RetrievalResult) -> RetrievalResult:
        return with_reported_trace(
            result,
            query=self.query,
            dense_fallback=first_reason(
                self.generation_ctx.get("dense_fallback"), self.generation_fallback
            ),
            generation_fallback=self.generation_fallback,
            legacy_fallback=self.legacy_fallback,
        )


def retrieve_via_search_memory(
    query: str,
    *,
    scope: str = "all",
    limit: int = 10,
    project: str | None = None,
    since: str | None = None,
    as_of: str | None = None,
    # The two entry points disagreed until 2026-08-25: `search_memory.search`
    # asks for the semantic leg, this one did not, so a caller reaching the
    # lower entry directly got lexical-only answers and no sign of it. Every
    # real caller passes the flag explicitly, so this only closes a trap.
    semantic: bool = True,
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
    trace_sink: dict[str, object] | None = None,
) -> list[dict[str, Any]]:
    """Public search path: independent backends → retrieve() → rows."""

    check_stopped(deadline_monotonic, cancelled)
    embedder, model_id, model_revision = resolved_query_encoder(
        semantic=semantic,
        embedder=generation_embedder,
        model_id=generation_model_id,
        model_revision=generation_model_revision,
    )
    analysis = retrieval_query.analyze_query(query)
    requested, wanted = planned_request(profile, analysis, semantic=semantic)
    run = _SearchRun(
        query,
        scope,
        limit,
        project,
        since,
        as_of,
        semantic,
        page_paths,
        graph,
        rerank,
        _selected_catalog(catalog),
        embedder,
        model_id,
        model_revision,
        deadline_monotonic,
        max_candidates,
        cancelled,
        requested,
        wanted,
    )
    run.open()
    result = run.reported(run.run())
    if trace_sink is not None:
        trace_sink.update(asdict(result.trace))
    rows = candidates_to_legacy(result, display_meta=result.display_meta)
    if emit_telemetry:
        record_impressions(
            rows, query=query, corpus_generation=run.corpus_generation, source_tool=source_tool
        )
    return rows
