"""Vector search, the model embedding and reranker adapters, and the deterministic fake reranker
and QA adapters."""
from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Sequence

import retrieval_v2_scoring
from retrieval_v2_contract import (
    ADAPTER_KIND,
    MODEL_MATRIX_ADAPTER_KIND,
    Candidate,
    ModelSelection,
    QueryScope,
    ScoredCandidate,
)
from retrieval_v2_corpus import filter_candidates
from retrieval_v2_scoring import candidate_text, dot, hash_vector
from retrieval_v2_transformers import fuse_rankings

# isort: split
from reliable_memory import canonical_json_bytes


class FakeEmbeddingAdapter:
    kind = ADAPTER_KIND

    def rank(
        self,
        query_text: str,
        scope: QueryScope,
        candidates: Sequence[Candidate],
        *,
        limit: int,
    ) -> list[ScoredCandidate]:
        query_vector = hash_vector(query_text)
        eligible = filter_candidates(candidates, scope)
        scored = [
            ScoredCandidate(candidate, dot(query_vector, hash_vector(candidate_text(candidate))))
            for candidate in eligible
        ]
        return sorted(scored, key=lambda item: (-item.score, item.evidence_id))[:limit]


def _numpy_or_error():
    try:
        import numpy as np
    except ImportError as exc:
        raise ValueError("model-matrix adapter requires the retrieval-benchmark extra") from exc
    return np


def _require_vector_rows(vectors, rows: int) -> None:
    if vectors.ndim != 2 or vectors.shape[0] != rows:
        raise ValueError("encoder returned an invalid vector matrix shape")


def _require_vector_dimension(vectors, dimensions: int, allow_truncation: bool) -> None:
    if vectors.shape[1] < dimensions or (not allow_truncation and vectors.shape[1] != dimensions):
        raise ValueError("encoder returned an invalid vector dimension")


def _unit_rows(np, vectors):
    if not np.isfinite(vectors).all():
        raise ValueError("encoder vectors must be finite")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise ValueError("encoder vectors must have nonzero norms")
    return np.ascontiguousarray(vectors / norms, dtype=np.float32)


def _prepare_embedding_vectors(raw, *, rows: int, dimensions: int, allow_truncation: bool):
    np = _numpy_or_error()
    vectors = np.asarray(raw)
    _require_vector_rows(vectors, rows)
    _require_vector_dimension(vectors, dimensions, allow_truncation)
    vectors = np.ascontiguousarray(vectors[:, :dimensions], dtype=np.float32)
    return _unit_rows(np, vectors)


def _numpy_exact_vectors(vectors, query, limit: int):
    import numpy as np

    scores = vectors @ query
    order = np.lexsort((np.arange(scores.shape[0]), -scores))[:limit]
    return order, scores[order]


def _native_usearch_search(vectors, query, limit: int, exact: bool):
    try:
        from usearch.index import Index
    except ImportError as exc:
        raise ValueError("USearch backend requires the retrieval-benchmark extra") from exc
    import numpy as np

    index = Index(ndim=vectors.shape[1], metric="cos", dtype="f32")
    keys = np.arange(vectors.shape[0], dtype=np.uint64)
    index.add(keys, vectors)
    matches = index.search(query, count=limit, exact=exact)
    return np.asarray(matches.keys), 1.0 - np.asarray(matches.distances, dtype=np.float32)


def _require_search_shapes(vectors, query, candidate_ids: Sequence[str]) -> None:
    if vectors.ndim != 2 or query.ndim != 1 or vectors.shape[1] != query.shape[0]:
        raise ValueError("vector search dimension mismatch")
    if len(candidate_ids) != vectors.shape[0]:
        raise ValueError("vector search candidate ID mismatch")


def _require_finite_search_inputs(np, vectors, query) -> None:
    if not np.isfinite(vectors).all() or not np.isfinite(query).all():
        raise ValueError("vector search inputs must be finite")


def _require_usearch_parity(np, keys, scores, reference) -> None:
    reference_keys, reference_scores = reference
    if not np.array_equal(keys, reference_keys) or not np.allclose(
        scores, reference_scores, rtol=1e-5, atol=1e-6
    ):
        raise ValueError("USearch exact parity check failed")


def _usearch_results(np, vectors, query, limit: int, backend: str, usearch_search, reference):
    search = usearch_search or _native_usearch_search
    keys, scores = search(vectors, query, limit, backend == "usearch-exact")
    keys = np.asarray(keys)
    scores = np.asarray(scores, dtype=np.float32)
    if backend == "usearch-exact":
        _require_usearch_parity(np, keys, scores, reference)
    return keys, scores


def _backend_results(np, vectors, query, limit: int, backend: str, usearch_search, reference):
    if backend in {"usearch-exact", "usearch-hnsw"}:
        return _usearch_results(np, vectors, query, limit, backend, usearch_search, reference)
    if backend != "numpy-exact":
        raise ValueError(f"unknown vector backend: {backend}")
    return reference


def _require_backend_results(np, keys, scores) -> None:
    if keys.ndim != 1 or scores.ndim != 1 or len(keys) != len(scores):
        raise ValueError("vector backend returned invalid results")
    if not np.isfinite(scores).all():
        raise ValueError("vector backend returned nonfinite scores")


def search_vectors(
    vectors,
    query,
    candidate_ids: Sequence[str],
    limit: int,
    *,
    backend: str,
    usearch_search=None,
) -> tuple[list[str], list[float]]:
    import numpy as np

    _require_search_shapes(vectors, query, candidate_ids)
    _require_finite_search_inputs(np, vectors, query)
    reference = _numpy_exact_vectors(vectors, query, limit)
    keys, scores = _backend_results(np, vectors, query, limit, backend, usearch_search, reference)
    _require_backend_results(np, keys, scores)
    return [candidate_ids[int(key)] for key in keys], [float(score) for score in scores]



def _split_encoder_signals(raw):
    if not isinstance(raw, dict):
        return raw, None
    if set(raw) != {"dense_vecs", "lexical_weights"}:
        raise ValueError("encoder returned unknown embedding signals")
    return raw["dense_vecs"], raw["lexical_weights"]


def _sparse_weight(token: object, weight: object) -> float:
    value = float(weight)
    if not isinstance(token, str) or not token or not math.isfinite(value) or value <= 0:
        raise ValueError("learned sparse token weights must be finite and positive")
    return value


def _normalized_sparse_row(row: object) -> dict[str, float]:
    if not isinstance(row, dict):
        raise ValueError("learned sparse output must contain token-weight mappings")
    return {token: _sparse_weight(token, weight) for token, weight in row.items()}


def _normalized_sparse(sparse: object, model_id: str, count: int) -> list[dict[str, float]]:
    if model_id != "BAAI/bge-m3" or not isinstance(sparse, list) or len(sparse) != count:
        raise ValueError("learned sparse output is not valid for this embedding")
    return [_normalized_sparse_row(row) for row in sparse]


def _learned_sparse_bytes(document_sparse: Sequence[dict[str, float]]) -> int:
    return sum(len(token.encode("utf-8")) + 8 for row in document_sparse for token in row)



class ModelEmbeddingAdapter:
    kind = MODEL_MATRIX_ADAPTER_KIND

    def __init__(
        self,
        selection: ModelSelection,
        *,
        encoder,
        vector_backend: str = "numpy-exact",
        usearch_search=None,
    ) -> None:
        self.selection = selection
        self._encoder = encoder
        self.vector_backend = vector_backend
        self._usearch_search = usearch_search
        self._candidate_ids: tuple[str, ...] | None = None
        self._candidate_by_id: dict[str, Candidate] = {}
        self.document_vectors = None
        self.document_sparse = None
        self.learned_sparse_bytes = 0
        self.last_trace: dict | None = None

    def _encode_signals(self, texts: Sequence[str]):
        model = self.selection.embedding
        inference = model["inference"]
        raw = self._encoder(
            texts,
            batch_size=model["batch_size"],
            max_length=inference["max_length_tokens"],
            pooling=inference["pooling"],
            padding_side=inference["padding_side"],
            truncation_side=inference["truncation_side"],
        )
        raw, sparse = _split_encoder_signals(raw)
        vectors = _prepare_embedding_vectors(
            raw,
            rows=len(texts),
            dimensions=self.selection.variant["dimensions"],
            allow_truncation=self.selection.variant["mrl"]["enabled"],
        )
        if sparse is not None:
            sparse = _normalized_sparse(sparse, model["id"], len(texts))
        return vectors, sparse

    def _encode(self, texts: Sequence[str]):
        return self._encode_signals(texts)[0]

    def _format(self, text: str, field: str) -> str:
        formatting = self.selection.embedding["formatting"]
        return formatting[field].format(text=text, instruction=formatting["instruction"])

    def _index_documents(self, candidates: Sequence[Candidate]) -> None:
        self._candidate_ids = tuple(candidate.evidence_id for candidate in candidates)
        self._candidate_by_id = {candidate.evidence_id: candidate for candidate in candidates}
        documents = [self._format(candidate_text(candidate), "document") for candidate in candidates]
        self.document_vectors, self.document_sparse = self._encode_signals(documents)
        if self.document_sparse is not None:
            self.learned_sparse_bytes = _learned_sparse_bytes(self.document_sparse)

    def _ensure_documents(self, candidates: Sequence[Candidate]) -> None:
        if self._candidate_ids is None:
            self._index_documents(candidates)
            return
        if tuple(candidate.evidence_id for candidate in candidates) != self._candidate_ids:
            raise ValueError("dense candidate universe mismatch")

    def _query_signals(self, formatted_query: str):
        # Encoded per query, as the product encodes it. Two dictionaries here
        # were read on every query and written nowhere, so the "cache" was a
        # guaranteed miss that only made the stand's cost look conditional.
        vectors, sparse = self._encode_signals([formatted_query])
        if sparse is None:
            return vectors[0], None
        return vectors[0], sparse[0]

    def _eligible_positions(self, candidates: Sequence[Candidate], scope: QueryScope):
        import numpy as np

        eligible_ids = {item.evidence_id for item in filter_candidates(candidates, scope)}
        indexes = np.asarray(
            [index for index, evidence_id in enumerate(self._candidate_ids) if evidence_id in eligible_ids],
            dtype=np.int64,
        )
        ids = tuple(self._candidate_ids[int(index)] for index in indexes)
        return indexes, ids

    def _dense_ranking(self, query_vector, indexes, ids: tuple[str, ...], limit: int) -> list[ScoredCandidate]:
        assert self.document_vectors is not None
        ranked_ids, scores = search_vectors(
            self.document_vectors[indexes],
            query_vector,
            ids,
            min(limit, len(ids)),
            backend=self.vector_backend,
            usearch_search=self._usearch_search,
        )
        return [
            ScoredCandidate(self._candidate_by_id[evidence_id], score)
            for evidence_id, score in zip(ranked_ids, scores)
        ]

    def _sparse_ranking(self, query_sparse: dict, indexes, ids: tuple[str, ...], limit: int) -> list[ScoredCandidate]:
        sparse_ranked = []
        for index, evidence_id in zip(indexes, ids):
            document = self.document_sparse[int(index)]
            score = sum(weight * document.get(token, 0.0) for token, weight in query_sparse.items())
            if score > 0:
                sparse_ranked.append(ScoredCandidate(self._candidate_by_id[evidence_id], score))
        sparse_ranked.sort(key=lambda item: (-item.score, item.evidence_id))
        return sparse_ranked[:limit]

    def rank(
        self,
        query_text: str,
        scope: QueryScope,
        candidates: Sequence[Candidate],
        *,
        limit: int,
    ) -> list[ScoredCandidate]:
        self._ensure_documents(candidates)
        assert self._candidate_ids is not None
        formatted_query = self._format(query_text, "query")
        query_vector, query_sparse = self._query_signals(formatted_query)
        indexes, ids = self._eligible_positions(candidates, scope)
        dense = self._dense_ranking(query_vector, indexes, ids, limit)
        if self.document_sparse is None:
            return dense
        if query_sparse is None:
            raise ValueError("BGE-M3 query omitted learned sparse output")
        sparse_ranked = self._sparse_ranking(query_sparse, indexes, ids, limit)
        fused = fuse_rankings((dense, sparse_ranked), list(self._candidate_by_id.values()), limit=limit)
        self.last_trace = _fusion_trace(dense, sparse_ranked)
        return fused


class ModelRerankerAdapter:
    kind = MODEL_MATRIX_ADAPTER_KIND

    def __init__(self, selection: ModelSelection, *, scorer) -> None:
        if selection.reranker is None:
            raise ValueError("reranker selection is required")
        self.selection = selection
        self._scorer = scorer
        self.last_trace: dict | None = None

    def _inputs(self, query_text: str, candidates: Sequence[ScoredCandidate]):
        formatting = self.selection.reranker["formatting"]
        if formatting["contract_type"] == "tokenizer_pair_sequence_classification":
            query = formatting["query_template"].format(query=query_text)
            return [
                (query, formatting["document_template"].format(document=item.text))
                for item in candidates
            ]
        return [{"query": query_text, "document": item.text} for item in candidates]

    def _depth_scores(self, query_text: str, prefix: list[ScoredCandidate]):
        reranker = self.selection.reranker
        scores = self._scorer(
            self._inputs(query_text, prefix),
            batch_size=reranker["batch_size"],
            max_length=reranker["formatting"]["max_length_tokens"],
            contract_type=reranker["formatting"]["contract_type"],
            score_tokens=reranker["formatting"]["score_tokens"],
        )
        if len(scores) != len(prefix) or any(not math.isfinite(float(score)) for score in scores):
            raise ValueError("reranker returned invalid or nonfinite scores")
        return scores

    @staticmethod
    def _rescored(prefix: list[ScoredCandidate], scores) -> list[ScoredCandidate]:
        rescored = [
            (index, ScoredCandidate(item.candidate, float(score)))
            for index, (item, score) in enumerate(zip(prefix, scores))
        ]
        rescored.sort(key=lambda pair: (-pair[1].score, pair[0]))
        return [item for _index, item in rescored]

    def rank_all(
        self,
        query_text: str,
        scope: QueryScope,
        candidates: Sequence[ScoredCandidate],
        *,
        limit: int,
    ) -> dict[int, list[ScoredCandidate]]:
        del scope
        frozen = list(candidates[:limit])
        frozen_ids = [item.evidence_id for item in frozen]
        fingerprint = hashlib.sha256(canonical_json_bytes(frozen_ids)).hexdigest()
        ranked_by_depth = {}
        depth_traces = {}
        for depth in self.selection.matrix["benchmark_contract"]["reranker_depths"]:
            prefix = frozen[:depth]
            started = time.perf_counter()
            scores = self._depth_scores(query_text, prefix)
            ranked_by_depth[depth] = self._rescored(prefix, scores) + frozen[depth:]
            depth_traces[str(depth)] = {
                "candidate_ids": frozen_ids[:depth],
                "duration_ms": (time.perf_counter() - started) * 1000,
                "fallback": None,
            }
        self.last_trace = {
            "model_id": self.selection.reranker["id"],
            "revision": self.selection.reranker["revision"],
            "variant_id": self.selection.reranker_variant["variant_id"],
            "formatting": json.loads(json.dumps(self.selection.reranker["formatting"])),
            "pre_rerank_candidate_ids": frozen_ids,
            "pre_rerank_fingerprint": fingerprint,
            "depths": depth_traces,
        }
        return ranked_by_depth


def _fusion_trace(dense: list[ScoredCandidate], sparse_ranked: list[ScoredCandidate]) -> dict:
    return {
        "dense_candidate_ids": [item.evidence_id for item in dense],
        "learned_sparse_candidate_ids": [item.evidence_id for item in sparse_ranked],
        "fusion": {"method": "reciprocal-rank-fusion", "k": 60},
    }


def _eligible_scored_ids(candidates: Sequence[ScoredCandidate], scope: QueryScope) -> set[str]:
    return {
        candidate.evidence_id
        for candidate in filter_candidates([item.candidate for item in candidates], scope)
    }


def _keep_best_scored(by_id: dict[str, ScoredCandidate], item: ScoredCandidate, score: float) -> None:
    current = by_id.get(item.evidence_id)
    if current is None or score > current.score:
        by_id[item.evidence_id] = ScoredCandidate(item.candidate, score)


class FakeRerankerAdapter:
    kind = ADAPTER_KIND

    def rank(
        self,
        query_text: str,
        scope: QueryScope,
        candidates: Sequence[ScoredCandidate],
        *,
        limit: int,
    ) -> list[ScoredCandidate]:
        eligible_ids = _eligible_scored_ids(candidates, scope)
        by_id: dict[str, ScoredCandidate] = {}
        for item in candidates:
            if item.evidence_id not in eligible_ids:
                continue
            score = max(item.score, 0.0) + 2.0 * retrieval_v2_scoring.lexical_score(query_text, item.candidate)
            _keep_best_scored(by_id, item, score)
        return sorted(by_id.values(), key=lambda item: (-item.score, item.evidence_id))[:limit]


class FakeQAAdapter:
    kind = ADAPTER_KIND

    def answer(
        self,
        query_text: str,
        scope: QueryScope,
        candidates: Sequence[ScoredCandidate],
    ) -> dict:
        del query_text, scope
        if not candidates or candidates[0].score < 0.54:
            return {"abstained": True, "reason": "not-in-corpus"}
        return {"abstained": False, "reason": None}
