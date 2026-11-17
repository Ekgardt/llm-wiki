"""The retrieval v2 adapters see no gold metadata and score only the frozen eligible candidates."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
from reliable_memory import canonical_json_bytes

from tests.retrieval_v2_helpers import CORPUS, SCHEMA, lexical_candidate

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import retrieval_v2_adapters  # noqa: E402
import retrieval_v2_contract  # noqa: E402
import retrieval_v2_corpus  # noqa: E402
import retrieval_v2_matrix  # noqa: E402
import retrieval_v2_scoring  # noqa: E402


def test_fake_adapter_interfaces_do_not_receive_gold_metadata():
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    candidates = retrieval_v2_corpus.build_candidates(corpus)
    scope = retrieval_v2_contract.QueryScope(projects=("aurora",), temporal_mode="current", as_of=None)
    lexical = retrieval_v2_scoring.FakeLexicalAdapter()
    embedding = retrieval_v2_adapters.FakeEmbeddingAdapter()
    reranker = retrieval_v2_adapters.FakeRerankerAdapter()
    qa = retrieval_v2_adapters.FakeQAAdapter()

    lexical_results = lexical.rank("cache cleanup command", scope, candidates, limit=50)
    embedding_results = embedding.rank("cache cleanup command", scope, candidates, limit=50)
    reranked = reranker.rank(
        "cache cleanup command", scope, lexical_results + embedding_results, limit=50
    )
    answer = qa.answer("cache cleanup command", scope, reranked)

    assert lexical_results and embedding_results and reranked
    assert set(answer) == {"abstained", "reason"}
    for adapter in (lexical, embedding, reranker, qa):
        assert not hasattr(adapter, "corpus")
        assert not hasattr(adapter, "queries")


def _sparse_weights(text: str) -> dict[str, float]:
    if "second" in text or text == "find it":
        return {"42": 1.0}
    return {"7": 1.0}


def _assert_depth_kept_frozen_tail(adapter, frozen_ids, depth: int, ranked) -> None:
    """Below its depth every list is the frozen order, unchanged."""
    assert [item.evidence_id for item in ranked[depth:]] == frozen_ids[depth:]
    assert adapter.last_trace["depths"][str(depth)]["candidate_ids"] == frozen_ids[:depth]


def test_adapters_never_score_candidates_excluded_by_hard_filters(monkeypatch):
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    candidates = retrieval_v2_corpus.build_candidates(corpus)
    scope = retrieval_v2_contract.QueryScope(projects=("aurora",), temporal_mode="current", as_of=None)
    eligible = {candidate.evidence_id for candidate in retrieval_v2_corpus.filter_candidates(candidates, scope)}
    scored = []
    original = retrieval_v2_scoring.lexical_score

    def observe(query_text, candidate):
        scored.append(candidate.evidence_id)
        return original(query_text, candidate)

    monkeypatch.setattr(retrieval_v2_scoring, "lexical_score", observe)
    retrieval_v2_scoring.FakeLexicalAdapter().rank("catalog", scope, candidates, limit=50)

    assert set(scored) == eligible

    reranker_scored = []

    def observe_reranker(query_text, candidate):
        reranker_scored.append(candidate.evidence_id)
        return original(query_text, candidate)

    monkeypatch.setattr(retrieval_v2_scoring, "lexical_score", observe_reranker)
    supplied = [retrieval_v2_contract.ScoredCandidate(candidate, 0.0) for candidate in candidates]
    retrieval_v2_adapters.FakeRerankerAdapter().rank("catalog", scope, supplied, limit=50)

    assert set(reranker_scored) == eligible


def test_embedding_adapter_applies_matrix_format_dimension_float32_and_no_gold():
    np = pytest.importorskip("numpy")
    selection = retrieval_v2_matrix.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="Qwen/Qwen3-Embedding-0.6B",
        variant_id="float32-384d",
    )
    calls = []

    def encoder(texts, **options):
        calls.append((tuple(texts), options))
        vectors = np.zeros((len(texts), 1024), dtype=np.float64)
        for row in range(len(texts)):
            vectors[row, row % 2] = 1.0
        return vectors

    candidates = [
        lexical_candidate("one", "first document"),
        lexical_candidate("two", "second document"),
    ]
    adapter = retrieval_v2_adapters.ModelEmbeddingAdapter(selection, encoder=encoder)
    ranked = adapter.rank(
        "find it",
        retrieval_v2_contract.QueryScope(projects=("alpha",), temporal_mode="current", as_of=None),
        candidates,
        limit=2,
    )

    assert calls[0][0] == tuple(retrieval_v2_scoring.candidate_text(item) for item in candidates)
    assert calls[1][0] == (
        "Instruct: Given a web search query, retrieve relevant passages that answer the query\n"
        "Query:find it",
    )
    assert all(call[1]["batch_size"] == 8 for call in calls)
    assert all(call[1]["max_length"] == 512 for call in calls)
    assert adapter.document_vectors.dtype == np.float32
    assert adapter.document_vectors.shape == (2, 384)
    assert len(ranked) == 2
    assert not hasattr(adapter, "queries") and not hasattr(adapter, "gold")


def test_bge_m3_adapter_fuses_learned_sparse_as_a_separate_signal():
    np = pytest.importorskip("numpy")
    selection = retrieval_v2_matrix.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-m3",
        variant_id="float32-1024d",
    )

    def encoder(texts, **options):
        del options
        dense = np.zeros((len(texts), 1024), dtype=np.float32)
        dense[:, 0] = 1.0
        sparse = [_sparse_weights(text) for text in texts]
        return {"dense_vecs": dense, "lexical_weights": sparse}

    candidates = [
        lexical_candidate("one", "first document"),
        lexical_candidate("two", "second document"),
    ]
    adapter = retrieval_v2_adapters.ModelEmbeddingAdapter(selection, encoder=encoder)
    ranked = adapter.rank(
        "find it",
        retrieval_v2_contract.QueryScope(projects=("alpha",), temporal_mode="current", as_of=None),
        candidates,
        limit=2,
    )

    assert [item.evidence_id for item in ranked] == ["two", "one"]
    assert adapter.last_trace["dense_candidate_ids"] == ["one", "two"]
    assert adapter.last_trace["learned_sparse_candidate_ids"] == ["two"]
    assert adapter.last_trace["fusion"] == {"method": "reciprocal-rank-fusion", "k": 60}
    assert adapter.learned_sparse_bytes > 0


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_embedding_adapter_rejects_nonfinite_and_invalid_vectors(bad):
    np = pytest.importorskip("numpy")
    selection = retrieval_v2_matrix.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )

    def encoder(texts, **options):
        del options
        vectors = np.ones((len(texts), 384), dtype=np.float32)
        vectors[0, 0] = bad
        return vectors

    adapter = retrieval_v2_adapters.ModelEmbeddingAdapter(selection, encoder=encoder)
    with pytest.raises(ValueError, match="finite"):
        adapter.rank(
            "query",
            retrieval_v2_contract.QueryScope(projects=("alpha",), temporal_mode="current", as_of=None),
            [lexical_candidate("one", "document")],
            limit=1,
        )


def test_usearch_exact_requires_numpy_id_and_score_parity():
    np = pytest.importorskip("numpy")
    vectors = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    query = np.asarray([1.0, 0.0], dtype=np.float32)

    def matching(_vectors, _query, limit, exact):
        assert exact is True
        return np.asarray([0, 1])[:limit], np.asarray([1.0, 0.0])[:limit]

    ids, scores = retrieval_v2_adapters.search_vectors(
        vectors, query, ("one", "two"), 2, backend="usearch-exact", usearch_search=matching
    )
    assert ids == ["one", "two"]
    assert scores == pytest.approx([1.0, 0.0])

    def mismatching(_vectors, _query, limit, exact):
        del limit, exact
        return np.asarray([1, 0]), np.asarray([1.0, 0.0])

    with pytest.raises(ValueError, match="parity"):
        retrieval_v2_adapters.search_vectors(
            vectors,
            query,
            ("one", "two"),
            2,
            backend="usearch-exact",
            usearch_search=mismatching,
        )


def test_reranker_runs_all_depths_from_one_frozen_candidate_list():
    selection = retrieval_v2_matrix.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        reranker_id="BAAI/bge-reranker-v2-m3",
    )
    candidates = [
        retrieval_v2_contract.ScoredCandidate(lexical_candidate(str(index), str(index)), 10 - index)
        for index in range(55)
    ]
    seen = []

    def scorer(inputs, **options):
        seen.append((list(inputs), options))
        return list(reversed(range(len(inputs))))

    adapter = retrieval_v2_adapters.ModelRerankerAdapter(selection, scorer=scorer)
    ranked_by_depth = adapter.rank_all(
        "query",
        retrieval_v2_contract.QueryScope(projects=("alpha",), temporal_mode="current", as_of=None),
        candidates,
        limit=55,
    )

    assert len(seen) == 3
    assert [len(inputs) for inputs, _options in seen] == [10, 20, 50]
    frozen_ids = [str(i) for i in range(55)]
    assert adapter.last_trace["pre_rerank_candidate_ids"] == frozen_ids
    assert adapter.last_trace["pre_rerank_fingerprint"] == hashlib.sha256(
        canonical_json_bytes(frozen_ids)
    ).hexdigest()
    assert set(adapter.last_trace["depths"]) == {"10", "20", "50"}
    for depth, ranked in ranked_by_depth.items():
        _assert_depth_kept_frozen_tail(adapter, frozen_ids, depth, ranked)
