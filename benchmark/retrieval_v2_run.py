"""Run one retrieval-v2 benchmark: its options, the per-run state and the report it returns."""
from __future__ import annotations

import gc
import hashlib
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import retrieval_v2_transformers
from retrieval_v2_adapters import (
    FakeEmbeddingAdapter,
    FakeQAAdapter,
    FakeRerankerAdapter,
    ModelEmbeddingAdapter,
    ModelRerankerAdapter,
)
from retrieval_v2_contract import (
    ADAPTER_KIND,
    DEFAULT_CORPUS,
    DEFAULT_LEXICAL_DEADLINE_SECONDS,
    DEFAULT_MATRIX,
    EFFECTIVENESS_FIELDS,
    LEXICAL_CONFIGURATIONS,
    MAX_CANDIDATES,
    MAX_CORPUS_BYTES,
    MODEL_MATRIX_ADAPTER_KIND,
    THRESHOLDS,
    ModelSelection,
    QueryScope,
    ScoredCandidate,
)
from retrieval_v2_corpus import build_candidates
from retrieval_v2_environment import environment_provenance, runner_sha256, sha256_file, sha256_json
from retrieval_v2_matrix import load_model_selection, matrix_target
from retrieval_v2_measurements import gate_results, process_peak_rss, resource_measurements
from retrieval_v2_metrics import (
    SHIPPABLE_LICENSES,
    aggregate,
    evaluation_row,
    expand_parents,
    language_slices,
    macro_average,
    query_scope,
)
from retrieval_v2_outputs import model_cache_environment, validate_cache_root, write_fake_index
from retrieval_v2_scoring import FakeLexicalAdapter
from retrieval_v2_sqlite_lexical import SQLiteLexicalAdapter
from retrieval_v2_transformers import fuse_rankings, load_transformer_reranker
from retrieval_v2_workspace import (
    cleanup_run_workspace,
    create_run_workspace,
    decorate_lexical_fallback,
    remove_semantic_artifacts,
)

# isort: split
from bounded_io import read_stable_bytes
from reliable_memory import canonical_json_bytes


def run_benchmark(
    corpus: dict,
    *,
    cache_root: Path | str,
    adapter: str = ADAPTER_KIND,
    corpus_path: Path | str = DEFAULT_CORPUS,
    matrix_path: Path | str = DEFAULT_MATRIX,
    model_id: str | None = None,
    variant_id: str | None = None,
    reranker_id: str | None = None,
    rerank_depth: int | None = None,
    vector_backend: str | None = None,
    allow_download: bool = False,
    lexical_config: str | None = None,
    test_segmenter=None,
    model_loader=None,
    encoder=None,
    reranker_loader=None,
    scorer=None,
    usearch_search=None,
    raw_output_written: bool = False,
    lexical_deadline_seconds: float = DEFAULT_LEXICAL_DEADLINE_SECONDS,
    clock=None,
) -> dict:
    if adapter != MODEL_MATRIX_ADAPTER_KIND:
        return _run_benchmark_once(
            corpus,
            cache_root=cache_root,
            adapter=adapter,
            corpus_path=corpus_path,
            matrix_path=matrix_path,
            model_id=model_id,
            variant_id=variant_id,
            reranker_id=reranker_id,
            rerank_depth=rerank_depth,
            vector_backend=vector_backend,
            allow_download=allow_download,
            lexical_config=lexical_config,
            test_segmenter=test_segmenter,
            model_loader=model_loader,
            encoder=encoder,
            reranker_loader=reranker_loader,
            scorer=scorer,
            usearch_search=usearch_search,
            raw_output_written=raw_output_written,
            lexical_deadline_seconds=lexical_deadline_seconds,
            clock=clock,
        )
    workspace = create_run_workspace(Path(cache_root))
    try:
        report = _run_benchmark_once(
            corpus,
            cache_root=workspace.path,
            model_cache_root=Path(cache_root).expanduser().resolve(strict=True),
            adapter=adapter,
            corpus_path=corpus_path,
            matrix_path=matrix_path,
            model_id=model_id,
            variant_id=variant_id,
            reranker_id=reranker_id,
            rerank_depth=rerank_depth,
            vector_backend=vector_backend,
            allow_download=allow_download,
            lexical_config=lexical_config,
            test_segmenter=test_segmenter,
            model_loader=model_loader,
            encoder=encoder,
            reranker_loader=reranker_loader,
            scorer=scorer,
            usearch_search=usearch_search,
            raw_output_written=raw_output_written,
            lexical_deadline_seconds=lexical_deadline_seconds,
            clock=clock,
        )
        report["methodology"]["cache_isolation"] = {
            "model_cache_root_identity": workspace.model_cache_identity,
            "model_cache_persistent": True,
            "run_workspace": "owner-only ephemeral child of cache-root/runs",
            "cleanup": "always after measurement",
        }
        return report
    finally:
        cleanup_run_workspace(workspace)


_METHODOLOGY_BASE = {
    "source": "https://ir-measur.es/en/latest/measures.html",
    "positive_retrieval_denominator": "answerable queries only",
    "recall": "fraction of all known relevant candidates retrieved",
    "all_required": "one only when every required evidence span is in top 20",
    "mrr": "reciprocal rank of first relevant evidence span through rank 10",
    "ndcg": "graded gain with log2 discount normalized by ideal ranking through rank 10",
    "false_answer_denominator": "unanswerable queries only",
    "unavailable_metrics": (
        "empty-denominator rates are represented as JSON null and excluded from macro means"
    ),
    "timing_and_rss": "measured and non-canonical",
    "phase_peak_rss": "process peak RSS is run-level; per-phase peaks are unavailable",
    "resource_unavailable": "unavailable measurements use JSON null plus measurement_status",
}


def _require_supported_adapter(adapter: str) -> None:
    if adapter not in {ADAPTER_KIND, MODEL_MATRIX_ADAPTER_KIND}:
        raise ValueError("only deterministic-fake or explicit model-matrix adapters are supported")


def _require_real_mode_choices(options: _RunOptions) -> None:
    if options.adapter != MODEL_MATRIX_ADAPTER_KIND:
        return
    _require_explicit_matrix_choices(options)


def _require_explicit_matrix_choices(options: _RunOptions) -> None:
    if not all((options.model_id, options.variant_id, options.lexical_config, options.vector_backend)):
        raise ValueError("model-matrix mode requires explicit model, variant, lexical, and vector choices")
    if options.rerank_depth is not None:
        raise ValueError("reranker evidence always runs matrix depths 10, 20, and 50 together")


def _require_known_lexical_config(lexical_config: str | None) -> None:
    if lexical_config is not None and lexical_config not in LEXICAL_CONFIGURATIONS:
        raise ValueError(f"unknown lexical configuration: {lexical_config}")


def _require_corpus_matches_selection(corpus: dict, selection: ModelSelection) -> None:
    if hashlib.sha256(canonical_json_bytes(corpus) + b"\n").hexdigest() != selection.corpus_sha256:
        raise ValueError("loaded corpus content does not match matrix corpus SHA256")


def _first_not_none(value, default):
    if value is not None:
        return value
    return default


def _empty_accelerator_caches() -> None:
    torch = sys.modules.get("torch")
    if torch is None:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    _empty_mps_cache(torch)


def _empty_mps_cache(torch) -> None:
    mps = getattr(torch, "mps", None)
    if mps is not None and mps.is_available():
        mps.empty_cache()


def _status_or(value: object, missing: str) -> str:
    if value is not None:
        return "measured"
    return missing


def _per_document(total: int, count: int) -> float:
    if count:
        return total / count
    return 0


def _claim_interpretation(quality_claim: bool) -> str:
    if quality_claim:
        return "real-model-quality"
    return "test-or-experimental"


def _selection_licenses_shippable(selection: ModelSelection) -> bool:
    if selection.embedding["license"] not in SHIPPABLE_LICENSES:
        return False
    return selection.reranker is None or selection.reranker["license"] in SHIPPABLE_LICENSES


def _reranker_target(selection: ModelSelection) -> dict | None:
    if selection.reranker is None:
        return None
    return matrix_target(selection.reranker, selection.reranker_variant)


def _ranked_evidence(ranking: Sequence[ScoredCandidate]) -> list[dict]:
    return [{"evidence_id": item.evidence_id, "score": round(item.score, 8)} for item in ranking]


class _RunOptions:
    """The caller's choices for one benchmark run, unchanged from the signature of _run_benchmark_once."""

    def __init__(self, **options) -> None:
        self.adapter = options["adapter"]
        self.corpus_path = options["corpus_path"]
        self.matrix_path = options["matrix_path"]
        self.model_id = options["model_id"]
        self.variant_id = options["variant_id"]
        self.reranker_id = options["reranker_id"]
        self.rerank_depth = options["rerank_depth"]
        self.vector_backend = options["vector_backend"]
        self.allow_download = options["allow_download"]
        self.lexical_config = options["lexical_config"]
        self.test_segmenter = options["test_segmenter"]
        self.model_loader = options["model_loader"]
        self.encoder = options["encoder"]
        self.reranker_loader = options["reranker_loader"]
        self.scorer = options["scorer"]
        self.usearch_search = options["usearch_search"]
        self.lexical_deadline_seconds = options["lexical_deadline_seconds"]
        self.clock = options["clock"]


class _QueryRanking:
    """What one query produced before its answer: the ranking, its traces, or a fallback report."""

    def __init__(
        self,
        ranked=None,
        *,
        pre_rerank_ids=None,
        embedding_signals=None,
        latency_ms: float | None = None,
    ) -> None:
        self.ranked = ranked
        self.pre_rerank_ids = pre_rerank_ids
        self.reranker_trace = None
        self.embedding_signals = embedding_signals
        self.latency_ms = latency_ms
        self.fallback: dict | None = None


class _BenchmarkRun:
    """One benchmark run: build, optional model stages, per-query evaluation, report.

    The stages keep the original order of clock reads, cleanup and fallback, so a
    report is byte-identical to the one the single function produced.
    """

    def __init__(self, corpus: dict, cache_root: Path, persistent_model_cache: Path, options: _RunOptions) -> None:
        self.corpus = corpus
        self.o = options
        self.cache_root = cache_root
        self.persistent_model_cache = persistent_model_cache
        self.real_mode = options.adapter == MODEL_MATRIX_ADAPTER_KIND
        self.effective_real_mode = self.real_mode
        self.candidates = build_candidates(corpus)
        self.phase_clock = options.clock or time.perf_counter
        self.sqlite_lexical: SQLiteLexicalAdapter | None = None
        self.lexical = None
        self.embedding = None
        self.reranker = None
        self.selection: ModelSelection | None = None
        self.acquisition_mode = None
        self.model_load_ms = None
        self.document_encoding_ms = None
        self.vector_bytes = None
        self.learned_sparse_bytes = None
        self.reranker_model_load_ms = None
        self.materialized_retrieval = None
        self.cache_context = None
        self.fallback_reason = None
        self.releasing_embedding = False
        self.index_size = 0
        self.qa = FakeQAAdapter()
        self.rows: list[dict] = []
        self.traces: list[dict] = []
        self.latencies: list[float] = []
        self.rerank_rows: dict[int, list[dict]] = {}

    # --- pipeline --------------------------------------------------------------

    def execute(self) -> dict:
        self.build_started = time.perf_counter()
        self._build_lexical()
        if self.effective_real_mode:
            self._load_selection()
            self._load_embedding()
        self.build_time_ms = (time.perf_counter() - self.build_started) * 1000
        self.rerank_rows = self._rerank_row_slots()
        for stage in (self._prepare_real_retrieval, self._evaluate_queries):
            fallback = stage()
            if fallback is not None:
                return fallback
        return self._report()

    # --- shared cleanup --------------------------------------------------------

    def _close_cache_context(self, exc_info) -> None:
        if self.cache_context is None:
            return
        self.cache_context.__exit__(*exc_info)
        self.cache_context = None

    def _discard_lexical_index(self) -> None:
        if self.sqlite_lexical is not None:
            self.sqlite_lexical._cleanup(remove=True)

    def _discard_lexical_index_and_artifacts(self) -> None:
        self.sqlite_lexical._cleanup(remove=True)
        remove_semantic_artifacts(self.cache_root.resolve(), self.sqlite_lexical.path)

    def _lexical_fallback_report(self, exc: Exception) -> dict:
        reason = f"{type(exc).__name__}: {exc}"
        fallback = run_benchmark(
            self.corpus,
            cache_root=self.cache_root,
            adapter=ADAPTER_KIND,
            lexical_config=self.o.lexical_config,
            test_segmenter=self.o.test_segmenter,
            lexical_deadline_seconds=self.o.lexical_deadline_seconds,
        )
        return decorate_lexical_fallback(
            fallback,
            selection=self.selection,
            lexical_config=self.o.lexical_config,
            vector_backend=self.o.vector_backend,
            acquisition_mode=self.acquisition_mode,
            reason=reason,
        )

    # --- build -----------------------------------------------------------------

    def _build_fake_indexes(self) -> None:
        self.index_size = write_fake_index(self.corpus, self.cache_root)
        self.lexical = FakeLexicalAdapter()
        self.embedding = FakeEmbeddingAdapter()
        self.reranker = FakeRerankerAdapter()

    def _build_sqlite_index(self, lexical_deadline: float) -> None:
        self.sqlite_lexical = SQLiteLexicalAdapter(
            self.candidates,
            self.cache_root,
            self.o.lexical_config,
            test_segmenter=self.o.test_segmenter,
            deadline=lexical_deadline,
        )
        self.lexical = self.sqlite_lexical
        self.index_size = self.sqlite_lexical.path.stat().st_size

    def _build_lexical(self) -> None:
        lexical_started = self.phase_clock()
        lexical_deadline = self.build_started + self.o.lexical_deadline_seconds
        if not self.real_mode and self.o.lexical_config is None:
            self._build_fake_indexes()
        else:
            self._build_sqlite_index(lexical_deadline)
        self.lexical_build_ms = (self.phase_clock() - lexical_started) * 1000

    def _load_selection(self) -> None:
        try:
            self.selection = load_model_selection(
                self.o.matrix_path,
                self.o.corpus_path,
                model_id=self.o.model_id,
                variant_id=self.o.variant_id,
                reranker_id=self.o.reranker_id,
            )
            _require_corpus_matches_selection(self.corpus, self.selection)
        except BaseException:
            self._discard_lexical_index()
            raise

    def _dense_encoder(self):
        if self.o.encoder is not None:
            return self.o.encoder
        loader = _first_not_none(self.o.model_loader, retrieval_v2_transformers.load_transformer_embedding)
        return loader(
            self.selection,
            cache_root=self.persistent_model_cache.resolve(),
            local_files_only=not self.o.allow_download,
            trust_remote_code=False,
        )

    def _build_embedding(self) -> None:
        self.cache_context = model_cache_environment(
            self.persistent_model_cache, allow_download=self.o.allow_download
        )
        self.acquisition_mode = self.cache_context.__enter__()
        load_started = self.phase_clock()
        self.embedding = ModelEmbeddingAdapter(
            self.selection,
            encoder=self._dense_encoder(),
            vector_backend=self.o.vector_backend,
            usearch_search=self.o.usearch_search,
        )
        self.model_load_ms = (self.phase_clock() - load_started) * 1000
        document_started = self.phase_clock()
        self.embedding._ensure_documents(self.candidates)
        self.document_encoding_ms = (self.phase_clock() - document_started) * 1000
        self.vector_bytes = int(self.embedding.document_vectors.nbytes)
        self.learned_sparse_bytes = self.embedding.learned_sparse_bytes
        self.index_size += self.vector_bytes + self.learned_sparse_bytes

    def _fall_back_to_lexical(self, exc: Exception) -> None:
        self.fallback_reason = f"{type(exc).__name__}: {exc}"
        self.effective_real_mode = False
        self.embedding = None
        self.reranker = None
        self.model_load_ms = None
        self.document_encoding_ms = None
        remove_semantic_artifacts(self.cache_root.resolve(), self.sqlite_lexical.path)
        self.index_size = self.sqlite_lexical.path.stat().st_size

    def _load_embedding(self) -> None:
        try:
            self._build_embedding()
        except Exception as exc:
            self._close_cache_context(sys.exc_info())
            self._fall_back_to_lexical(exc)
        except BaseException:
            self._close_cache_context(sys.exc_info())
            self._discard_lexical_index_and_artifacts()
            raise

    def _rerank_row_slots(self) -> dict[int, list[dict]]:
        if not self.real_mode:
            return {}
        return {depth: [] for depth in self.selection.matrix["benchmark_contract"]["reranker_depths"]}

    # --- real-mode retrieval before evaluation ---------------------------------

    def _materialized_query(self, np, query: dict) -> dict:
        started = time.perf_counter()
        scope = query_scope(query)
        lexical_ranked = self.sqlite_lexical.rank(
            query["text"], scope, self.candidates, limit=MAX_CANDIDATES, language=query["language"]
        )
        embedding_ranked = self.embedding.rank(query["text"], scope, self.candidates, limit=MAX_CANDIDATES)
        fused = fuse_rankings((lexical_ranked, embedding_ranked), self.candidates, limit=MAX_CANDIDATES)
        return {
            "candidate_ids": tuple(item.evidence_id for item in fused),
            "scores": np.asarray([item.score for item in fused], dtype=np.float32),
            "embedding_signals": json.loads(json.dumps(self.embedding.last_trace)),
            "duration_ms": (time.perf_counter() - started) * 1000,
        }

    def _materialize_retrieval(self) -> None:
        import numpy as np

        self.materialized_retrieval = []
        for query in self.corpus["queries"]:
            self.materialized_retrieval.append(self._materialized_query(np, query))

    def _release_embedding(self) -> None:
        self.embedding._encoder = None
        self.embedding = None
        self.releasing_embedding = True
        gc.collect()
        _empty_accelerator_caches()
        self.releasing_embedding = False

    def _reranker_scorer(self):
        if self.o.scorer is not None:
            return self.o.scorer
        loader = _first_not_none(self.o.reranker_loader, load_transformer_reranker)
        return loader(
            self.selection,
            cache_root=self.persistent_model_cache.resolve(),
            local_files_only=not self.o.allow_download,
            trust_remote_code=False,
        )

    def _load_reranker(self) -> None:
        if self.o.reranker_id is None:
            return
        reranker_load_started = self.phase_clock()
        self.reranker = ModelRerankerAdapter(self.selection, scorer=self._reranker_scorer())
        self.reranker_model_load_ms = (self.phase_clock() - reranker_load_started) * 1000
        self.model_load_ms += self.reranker_model_load_ms

    def _prepare_real_retrieval(self) -> dict | None:
        if not self.effective_real_mode:
            return None
        self.releasing_embedding = False
        try:
            self._materialize_retrieval()
            self._release_embedding()
            self._load_reranker()
        except Exception as exc:
            self._close_cache_context(sys.exc_info())
            self._discard_lexical_index_and_artifacts()
            if self.releasing_embedding:
                raise
            return self._lexical_fallback_report(exc)
        except BaseException:
            self._close_cache_context(sys.exc_info())
            self._discard_lexical_index_and_artifacts()
            raise
        return None

    # --- per-query evaluation --------------------------------------------------

    def _fake_ranking(self, query: dict, scope: QueryScope) -> _QueryRanking:
        lexical_ranked = self.lexical.rank(query["text"], scope, self.candidates, limit=MAX_CANDIDATES)
        assert self.embedding is not None and self.reranker is not None
        embedding_ranked = self.embedding.rank(query["text"], scope, self.candidates, limit=MAX_CANDIDATES)
        ranked = self.reranker.rank(query["text"], scope, lexical_ranked + embedding_ranked, limit=MAX_CANDIDATES)
        return _QueryRanking(ranked)

    def _record_depth(self, query: dict, scope: QueryScope, depth: int, depth_ranking, retrieval_ms: float) -> None:
        depth_trace = self.reranker.last_trace["depths"][str(depth)]
        depth_trace["ranked_evidence"] = _ranked_evidence(depth_ranking)
        depth_trace["query_latency_ms"] = retrieval_ms + depth_trace["duration_ms"]
        depth_answer = self.qa.answer(query["text"], scope, depth_ranking)
        depth_trace["abstained"] = depth_answer["abstained"]
        depth_trace["abstention_reason"] = depth_answer["reason"]
        self.rerank_rows[depth].append(evaluation_row(query, depth_ranking, depth_answer))

    def _rerank(self, query: dict, scope: QueryScope, ranking: _QueryRanking, retrieval_ms: float) -> None:
        if self.reranker is None:
            return
        reranking_started = time.perf_counter()
        ranked_by_depth = self.reranker.rank_all(query["text"], scope, ranking.ranked, limit=MAX_CANDIDATES)
        for depth, depth_ranking in ranked_by_depth.items():
            self._record_depth(query, scope, depth, depth_ranking, retrieval_ms)
        ranking.ranked = ranked_by_depth[max(ranked_by_depth)]
        ranking.reranker_trace = json.loads(json.dumps(self.reranker.last_trace))
        ranking.latency_ms = retrieval_ms + (time.perf_counter() - reranking_started) * 1000

    def _real_ranking(self, query_index: int, query: dict, scope: QueryScope) -> _QueryRanking:
        frozen = self.materialized_retrieval[query_index]
        ranked = [
            ScoredCandidate(self.sqlite_lexical._candidates[evidence_id], float(score))
            for evidence_id, score in zip(frozen["candidate_ids"], frozen["scores"])
        ]
        ranking = _QueryRanking(
            ranked,
            pre_rerank_ids=list(frozen["candidate_ids"]),
            embedding_signals=frozen["embedding_signals"],
            latency_ms=frozen["duration_ms"],
        )
        try:
            self._rerank(query, scope, ranking, frozen["duration_ms"])
        except Exception as exc:
            self._close_cache_context(sys.exc_info())
            self._discard_lexical_index_and_artifacts()
            ranking.fallback = self._lexical_fallback_report(exc)
        except BaseException:
            self._close_cache_context(sys.exc_info())
            self._discard_lexical_index_and_artifacts()
            raise
        return ranking

    def _query_ranking(self, query_index: int, query: dict, scope: QueryScope) -> _QueryRanking:
        if not self.real_mode and self.sqlite_lexical is None:
            return self._fake_ranking(query, scope)
        if self.effective_real_mode:
            return self._real_ranking(query_index, query, scope)
        ranked = self.sqlite_lexical.rank(
            query["text"], scope, self.candidates, limit=MAX_CANDIDATES, language=query["language"]
        )
        return _QueryRanking(ranked)

    def _query_latency(self, ranking: _QueryRanking, started: float) -> float:
        if self.effective_real_mode:
            return ranking.latency_ms
        return (time.perf_counter() - started) * 1000

    def _record_query(self, query: dict, scope: QueryScope, ranking: _QueryRanking, started: float) -> None:
        answer = self.qa.answer(query["text"], scope, ranking.ranked)
        latency_ms = self._query_latency(ranking, started)
        self.latencies.append(latency_ms)
        ranked_parents = expand_parents(ranking.ranked)
        row = evaluation_row(query, ranking.ranked, answer)
        self.rows.append(row)
        trace = {
            "query_id": query["query_id"],
            "ranked_evidence": _ranked_evidence(ranking.ranked),
            "ranked_parents": ranked_parents,
            "abstained": answer["abstained"],
            "abstention_reason": answer["reason"],
            "abstention_contract_valid": row["abstention_contract_valid"],
            "latency_ms": round(latency_ms, 6),
        }
        if self.real_mode:
            trace["pre_rerank_candidate_ids"] = ranking.pre_rerank_ids
            trace["reranker"] = ranking.reranker_trace
            trace["embedding_signals"] = ranking.embedding_signals
        self.traces.append(trace)

    def _evaluate_query(self, query_index: int, query: dict) -> dict | None:
        started = time.perf_counter()
        scope = query_scope(query)
        ranking = self._query_ranking(query_index, query, scope)
        if ranking.fallback is not None:
            return ranking.fallback
        self._record_query(query, scope, ranking, started)
        return None

    def _finish_evaluation(self, succeeded: bool, exc_info) -> None:
        if self.sqlite_lexical is not None:
            self.sqlite_lexical._cleanup(remove=not succeeded)
        if self.cache_context is not None:
            self.cache_context.__exit__(*exc_info)

    def _evaluate_queries(self) -> dict | None:
        succeeded = False
        try:
            fallback = self._first_query_fallback()
            if fallback is not None:
                return fallback
            succeeded = True
        finally:
            self._finish_evaluation(succeeded, sys.exc_info())
        return None

    def _first_query_fallback(self) -> dict | None:
        for query_index, query in enumerate(self.corpus["queries"]):
            fallback = self._evaluate_query(query_index, query)
            if fallback is not None:
                return fallback
        return None

    # --- report ----------------------------------------------------------------

    def _indexing_duration_ms(self) -> float | None:
        if self.effective_real_mode:
            return self.document_encoding_ms
        return self.lexical_build_ms

    def _add_model_measurements(self, measurements: dict) -> None:
        measurements["model_load_ms"] = self.model_load_ms
        measurements["reranker_model_load_ms"] = self.reranker_model_load_ms
        measurements["document_encoding_index_build_ms"] = self.document_encoding_ms
        measurements["vector_bytes"] = self.vector_bytes
        measurements["learned_sparse_bytes"] = self.learned_sparse_bytes
        measurements["vector_bytes_per_document"] = _per_document(self.vector_bytes, len(self.candidates))
        measurements["measurement_status"].update(
            model_load_ms=_status_or(self.model_load_ms, "unavailable"),
            reranker_model_load_ms=_status_or(self.reranker_model_load_ms, "not-applicable"),
            document_encoding_index_build_ms=_status_or(self.document_encoding_ms, "unavailable"),
            vector_bytes="measured",
            learned_sparse_bytes=_status_or(self.learned_sparse_bytes, "not-applicable"),
            vector_bytes_per_document="measured",
        )

    def _measurements(self) -> dict:
        peak_rss, peak_rss_status = process_peak_rss()
        measurements = resource_measurements(
            self.latencies,
            build_time_ms=self.build_time_ms,
            indexing_duration_ms=self._indexing_duration_ms(),
            chunk_count=len(self.candidates),
            index_size_bytes=self.index_size,
            peak_rss_bytes=peak_rss,
            peak_rss_status=peak_rss_status,
        )
        measurements["lexical_build_ms"] = self.lexical_build_ms
        measurements["measurement_status"]["lexical_build_ms"] = "measured"
        if self.effective_real_mode:
            self._add_model_measurements(measurements)
        return measurements

    def _reported_vector_backend(self) -> str | None:
        if self.real_mode:
            return self.o.vector_backend
        return None

    def _retrieval_order(self) -> str:
        if self.effective_real_mode:
            return "independent BM25 and dense generation, RRF, then all frozen-prefix rerank depths"
        return "BM25-only lexical ablation; dense models not invoked"

    def _reranked(self) -> bool:
        if not self.effective_real_mode:
            return False
        return self.selection.reranker is not None

    def _reranker_confidence(self) -> str | None:
        if not self._reranked():
            return None
        formatting = self.selection.reranker["formatting"]
        if formatting["contract_type"] == "tokenizer_pair_sequence_classification":
            return "sigmoid_probability_from_sequence_classification_logit"
        return formatting["scoring"]

    def _add_lexical_methodology(self, methodology: dict) -> None:
        methodology["lexical_configuration"] = json.loads(json.dumps(LEXICAL_CONFIGURATIONS[self.o.lexical_config]))
        methodology["retrieval_order"] = self._retrieval_order()
        methodology["segmentation_runtime"] = self.sqlite_lexical.segmentation_runtime
        methodology["confidence"] = {
            "fusion": "RRF divided by its theoretical maximum; bounded to [0,1]",
            "qa_threshold": 0.54,
            "reranker": self._reranker_confidence(),
        }

    def _methodology(self) -> dict:
        methodology = dict(_METHODOLOGY_BASE)
        methodology["environment_provenance"] = environment_provenance(self._reported_vector_backend())
        if self.o.lexical_config is not None:
            self._add_lexical_methodology(methodology)
        return methodology

    def _add_real_mode_gates(self, gates: dict, measurements: dict, quality_claim: bool) -> None:
        status = measurements["measurement_status"]
        gates["interpretation"] = _claim_interpretation(quality_claim)
        gates["shipping_eligible"] = True
        gates["overall"] = gates["passed_for_orchestration"]
        gates["per_language"] = dict(gates["language_results"])
        gates["no_parent_recall_at_10_regression"] = gates["metric_results"]["parent_recall_at_10"]
        gates["required"] = {
            "every_language_gate": all(gates["language_results"].values()),
            "latency": status["warm_latency_p95_ms"] == "measured",
            "license": _selection_licenses_shippable(self.selection),
            "no_parent_recall_at_10_regression": gates["metric_results"]["parent_recall_at_10"],
            "ram": status["peak_rss_bytes"] == "measured",
        }
        gates["degraded"] = not self.effective_real_mode
        gates["release_evidence"] = False

    def _gates(self, overall: dict, slices: dict, measurements: dict, quality_claim: bool) -> dict:
        gates = gate_results(
            overall,
            slices,
            qa_contract_passed=all(row["abstention_contract_valid"] for row in self.rows),
        )
        if self.real_mode:
            self._add_real_mode_gates(gates, measurements, quality_claim)
        return gates

    def _depth_metric(self, depth: int, depth_rows: list[dict]) -> dict:
        depth_traces = [trace["reranker"]["depths"][str(depth)] for trace in self.traces]
        return {
            "overall": aggregate(depth_rows),
            "slices": language_slices(depth_rows),
            "duration_ms": sum(depth_trace["duration_ms"] for depth_trace in depth_traces),
            "inference_latencies_ms": [depth_trace["duration_ms"] for depth_trace in depth_traces],
            "warm_latency_p95_ms": resource_measurements(
                [depth_trace["query_latency_ms"] for depth_trace in depth_traces],
                build_time_ms=0,
                chunk_count=0,
                index_size_bytes=0,
                peak_rss_bytes=None,
                peak_rss_status="shared-run-level",
            )["warm_latency_p95_ms"],
            "shared_resources": ["peak_rss_bytes", "index_size_bytes", "vector_bytes"],
        }

    def _depth_metrics(self) -> dict:
        if not self._reranked():
            return {}
        return {str(depth): self._depth_metric(depth, depth_rows) for depth, depth_rows in self.rerank_rows.items()}

    def _lexical_matrix(self) -> dict | None:
        if self.o.lexical_config is None or self.real_mode:
            return None
        matrix_raw = read_stable_bytes(Path(self.o.matrix_path), MAX_CORPUS_BYTES, label="model matrix")
        lexical_matrix = json.loads(matrix_raw)
        self._require_frozen_lexical_matrix(lexical_matrix, matrix_raw, Path(self.o.corpus_path))
        return lexical_matrix

    @staticmethod
    def _require_frozen_lexical_matrix(lexical_matrix: dict, matrix_raw: bytes, corpus_path: Path) -> None:
        if canonical_json_bytes(lexical_matrix) + b"\n" != matrix_raw:
            raise ValueError("model matrix bytes are not canonical and frozen")
        if sha256_file(corpus_path) != lexical_matrix["benchmark_contract"]["corpus"]["sha256"]:
            raise ValueError("lexical ablation corpus does not match the benchmark contract")

    def _adapter_kind(self) -> str:
        if self.real_mode:
            return MODEL_MATRIX_ADAPTER_KIND
        if self.sqlite_lexical is not None:
            return self.sqlite_lexical.kind
        return ADAPTER_KIND

    def _model_identity(self) -> dict:
        if not self.real_mode:
            return {"model_id": None, "variant_id": None, "revision": None}
        return {
            "model_id": self.selection.embedding["id"],
            "variant_id": self.selection.variant["variant_id"],
            "revision": self.selection.embedding["revision"],
        }

    def _input_hashes(self, lexical_matrix: dict | None) -> dict:
        if self.real_mode:
            return {"matrix_sha256": self.selection.matrix_sha256, "corpus_sha256": self.selection.corpus_sha256}
        if lexical_matrix is None:
            return {"matrix_sha256": None, "corpus_sha256": None}
        return {
            "matrix_sha256": sha256_file(Path(self.o.matrix_path)),
            "corpus_sha256": sha256_file(Path(self.o.corpus_path)),
        }

    def _reranker_report(self, depth_metrics: dict) -> dict | None:
        if not self._reranked():
            return None
        reranker = self.selection.reranker
        return {
            "model_id": reranker["id"],
            "revision": reranker["revision"],
            "variant_id": self.selection.reranker_variant["variant_id"],
            "depths": list(self.selection.matrix["benchmark_contract"]["reranker_depths"]),
            "depth_metrics": depth_metrics,
            "formatting": reranker["formatting"],
        }

    def _requested_mode(self) -> str:
        if self.real_mode:
            return MODEL_MATRIX_ADAPTER_KIND
        return ADAPTER_KIND

    def _effective_mode(self) -> str:
        if self.effective_real_mode:
            return MODEL_MATRIX_ADAPTER_KIND
        if self.real_mode:
            return f"lexical-{self.o.lexical_config}"
        return ADAPTER_KIND

    def _benchmark_contract(self, lexical_matrix: dict | None) -> dict | None:
        if self.real_mode:
            return self.selection.matrix["benchmark_contract"]
        if lexical_matrix is not None:
            return lexical_matrix["benchmark_contract"]
        return None

    def _contract_hashes(self, lexical_matrix: dict | None) -> dict:
        contract = self._benchmark_contract(lexical_matrix)
        if contract is None:
            return {"benchmark_contract_sha256": None, "benchmark_runner_sha256": None}
        return {"benchmark_contract_sha256": sha256_json(contract), "benchmark_runner_sha256": runner_sha256()}

    def _candidate_report(self) -> dict | None:
        if not self.real_mode:
            return None
        return {
            "embedding": matrix_target(self.selection.embedding, self.selection.variant),
            "reranker": _reranker_target(self.selection),
        }

    def _report(self) -> dict:
        quality_claim = False
        release_evidence = False
        overall = aggregate(self.rows)
        slices = language_slices(self.rows)
        macro = {metric: macro_average(slices, metric) for metric in EFFECTIVENESS_FIELDS}
        measurements = self._measurements()
        methodology = self._methodology()
        gates = self._gates(overall, slices, measurements, quality_claim)
        depth_metrics = self._depth_metrics()
        lexical_matrix = self._lexical_matrix()
        return {
            "schema_version": "retrieval-report/v2",
            "corpus_id": self.corpus["corpus_id"],
            "adapter_kind": self._adapter_kind(),
            "quality_claim": quality_claim,
            "methodology": methodology,
            "overall": overall,
            "slices": slices,
            "macro_average": macro,
            "thresholds": dict(THRESHOLDS),
            "gates": gates,
            "measurements": measurements,
            "traces": self.traces,
            **self._model_identity(),
            **self._input_hashes(lexical_matrix),
            "acquisition_mode": self.acquisition_mode,
            "vector_backend": self._reported_vector_backend(),
            "reranker": self._reranker_report(depth_metrics),
            "release_evidence": release_evidence,
            "requested_mode": self._requested_mode(),
            "effective_mode": self._effective_mode(),
            "fallback_reason": self.fallback_reason,
            **self._contract_hashes(lexical_matrix),
            "candidate": self._candidate_report(),
        }


def _persistent_model_cache(cache_root: Path, model_cache_root: Path | str | None) -> Path:
    if model_cache_root is None:
        return validate_cache_root(cache_root)
    return validate_cache_root(Path(model_cache_root))


def _run_benchmark_once(
    corpus: dict,
    *,
    cache_root: Path | str,
    adapter: str = ADAPTER_KIND,
    corpus_path: Path | str = DEFAULT_CORPUS,
    matrix_path: Path | str = DEFAULT_MATRIX,
    model_id: str | None = None,
    variant_id: str | None = None,
    reranker_id: str | None = None,
    rerank_depth: int | None = None,
    vector_backend: str | None = None,
    allow_download: bool = False,
    lexical_config: str | None = None,
    test_segmenter=None,
    model_loader=None,
    encoder=None,
    reranker_loader=None,
    scorer=None,
    usearch_search=None,
    raw_output_written: bool = False,
    lexical_deadline_seconds: float = DEFAULT_LEXICAL_DEADLINE_SECONDS,
    clock=None,
    model_cache_root: Path | str | None = None,
) -> dict:
    """Run retrieval without exposing query gold metadata to any adapter."""
    del raw_output_written
    options = _RunOptions(
        adapter=adapter,
        corpus_path=corpus_path,
        matrix_path=matrix_path,
        model_id=model_id,
        variant_id=variant_id,
        reranker_id=reranker_id,
        rerank_depth=rerank_depth,
        vector_backend=vector_backend,
        allow_download=allow_download,
        lexical_config=lexical_config,
        test_segmenter=test_segmenter,
        model_loader=model_loader,
        encoder=encoder,
        reranker_loader=reranker_loader,
        scorer=scorer,
        usearch_search=usearch_search,
        lexical_deadline_seconds=lexical_deadline_seconds,
        clock=clock,
    )
    _require_supported_adapter(adapter)
    _require_real_mode_choices(options)
    _require_known_lexical_config(lexical_config)
    validated_cache_root = validate_cache_root(Path(cache_root))
    persistent_model_cache = _persistent_model_cache(validated_cache_root, model_cache_root)
    return _BenchmarkRun(corpus, validated_cache_root, persistent_model_cache, options).execute()
