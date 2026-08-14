"""Tests for the cross-encoder reranker module (reranker.py)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from reranker import (  # noqa: E402
    _sigmoid,
    rerank,
    reranker_available,
    should_rerank,
)


def _reset_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    import reranker

    monkeypatch.setattr(reranker, "_reranker_bundle", None)


def test_no_environment_configuration_means_no_reranker_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import reranker

    monkeypatch.delenv("LLMWIKI_RERANKER_MODEL", raising=False)
    monkeypatch.delenv("LLMWIKI_RERANKER_REVISION", raising=False)
    _reset_bundle(monkeypatch)
    monkeypatch.setattr(
        reranker,
        "_have_reranker_deps",
        lambda: pytest.fail("dependency probe ran without a configured identity"),
    )

    assert reranker.configured_reranker_identity() is None
    assert reranker._get_reranker_bundle() is None


@pytest.mark.parametrize(
    ("model", "revision"),
    [
        ("model", ""),
        ("", "a" * 40),
        ("model", "main"),
        ("model", "v1"),
        ("model", "A" * 40),
    ],
)
def test_partial_or_mutable_identity_is_rejected(
    monkeypatch: pytest.MonkeyPatch, model: str, revision: str
) -> None:
    import reranker

    monkeypatch.setenv("LLMWIKI_RERANKER_MODEL", model)
    monkeypatch.setenv("LLMWIKI_RERANKER_REVISION", revision)
    _reset_bundle(monkeypatch)

    assert reranker.configured_reranker_identity() is None


def _fake_reranker_modules(monkeypatch: pytest.MonkeyPatch, calls: list[tuple]) -> None:
    optimum = ModuleType("optimum")
    optimum.__path__ = []
    optimum_onnx = ModuleType("optimum.onnxruntime")
    transformers = ModuleType("transformers")

    class Model:
        @classmethod
        def from_pretrained(cls, model: str, **options):
            calls.append(("model", model, options))
            return object()

    class Tokenizer:
        @classmethod
        def from_pretrained(cls, model: str, **options):
            calls.append(("tokenizer", model, options))
            return object()

    optimum_onnx.ORTModelForSequenceClassification = Model
    transformers.AutoTokenizer = Tokenizer
    monkeypatch.setitem(sys.modules, "optimum", optimum)
    monkeypatch.setitem(sys.modules, "optimum.onnxruntime", optimum_onnx)
    monkeypatch.setitem(sys.modules, "transformers", transformers)


def test_explicit_immutable_model_load_is_local_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import reranker

    revision = "a" * 40
    monkeypatch.setenv("LLMWIKI_RERANKER_MODEL", "org/model")
    monkeypatch.setenv("LLMWIKI_RERANKER_REVISION", revision)
    _reset_bundle(monkeypatch)
    monkeypatch.setattr(reranker, "_have_reranker_deps", lambda: True)
    calls: list[tuple] = []
    _fake_reranker_modules(monkeypatch, calls)

    bundle = reranker._get_reranker_bundle()

    assert bundle is not None
    assert bundle["model_id"] == "org/model"
    assert bundle["model_revision"] == revision
    assert calls == [
        (
            "model",
            "org/model",
            {
                "file_name": "onnx/model.onnx",
                "revision": revision,
                "local_files_only": True,
                "trust_remote_code": False,
            },
        ),
        (
            "tokenizer",
            "org/model",
            {
                "revision": revision,
                "local_files_only": True,
                "trust_remote_code": False,
            },
        ),
    ]


def test_missing_local_artifact_degrades_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import reranker

    revision = "b" * 40
    monkeypatch.setenv("LLMWIKI_RERANKER_MODEL", "org/missing")
    monkeypatch.setenv("LLMWIKI_RERANKER_REVISION", revision)
    _reset_bundle(monkeypatch)
    monkeypatch.setattr(reranker, "_have_reranker_deps", lambda: True)
    calls: list[tuple] = []
    _fake_reranker_modules(monkeypatch, calls)

    def missing(model: str, **options):
        calls.append(("missing", model, options))
        raise OSError("not cached")

    sys.modules[
        "optimum.onnxruntime"
    ].ORTModelForSequenceClassification.from_pretrained = missing

    assert reranker._get_reranker_bundle() is None
    assert len(calls) == 1
    assert calls[0][2]["local_files_only"] is True
    assert calls[0][2]["trust_remote_code"] is False


class TestGracefulDegradation:
    def test_reranker_available_returns_bool(self):
        assert isinstance(reranker_available(), bool)

    def test_rerank_returns_input_when_deps_unavailable(self):
        docs = [{"slug": "a", "score": 1.0}, {"slug": "b", "score": 0.5}]
        with patch("reranker._get_reranker_bundle", return_value=None):
            result = rerank("query", docs, limit=10)
        assert [d["slug"] for d in result] == ["a", "b"]
        assert result[0]["reranker_applied"] is False

    def test_rerank_handles_empty_documents(self):
        assert rerank("query", [], limit=10) == []

    def test_rerank_handles_empty_query(self):
        docs = [{"slug": "a", "score": 1.0}]
        result = rerank("", docs, limit=10)
        assert result == docs


class TestFakeCrossEncoder:
    def test_rerank_reorders_by_fake_scores(self):
        docs = [
            {
                "slug": "low",
                "candidate_id": "low",
                "title": "Unrelated",
                "summary": "Nothing",
                "score": 0.9,
                "rrf_score": 0.9,
            },
            {
                "slug": "high",
                "candidate_id": "high",
                "title": "Perfect Match",
                "summary": "Exactly relevant",
                "score": 0.3,
                "rrf_score": 0.3,
            },
        ]

        def fake_scorer(pairs):
            # Second document is far more relevant.
            assert len(pairs) == 2
            return [0.1, 5.0]

        result = rerank("query", docs, limit=10, scorer=fake_scorer)
        assert result[0]["slug"] == "high"
        assert result[0]["reranker_applied"] is True
        assert result[0]["reranker_model_id"] == "fake-cross-encoder"
        assert result[0]["final_score"] >= result[1]["final_score"]
        assert result[0]["score"] == result[0]["final_score"]

    def test_rerank_preserves_tail_beyond_depth(self):
        docs = [
            {
                "slug": f"p{i}",
                "candidate_id": f"p{i}",
                "title": f"Page {i}",
                "summary": "x",
                "score": 1.0 - i * 0.01,
                "rrf_score": 1.0 - i * 0.01,
            }
            for i in range(10)
        ]

        def fake_scorer(pairs):
            # Reverse the reranked prefix.
            return list(range(len(pairs), 0, -1))

        result = rerank("query", docs, limit=8, depth=3, scorer=fake_scorer)
        assert len(result) == 8
        # Prefix of 3 was reranked; remaining 5 keep original order after prefix.
        tail_slugs = [d["slug"] for d in result[3:]]
        assert tail_slugs == ["p3", "p4", "p5", "p6", "p7"]
        assert all(d.get("reranker_applied") for d in result)

    def test_rerank_blends_normalized_score_with_rrf(self):
        docs = [
            {
                "slug": "a",
                "candidate_id": "a",
                "summary": "a",
                "rrf_score": 0.5,
                "score": 0.5,
            },
            {
                "slug": "b",
                "candidate_id": "b",
                "summary": "b",
                "rrf_score": 0.5,
                "score": 0.5,
            },
        ]

        def fake_scorer(_pairs):
            return [0.0, 10.0]

        result = rerank("q", docs, limit=2, scorer=fake_scorer)
        assert result[0]["slug"] == "b"
        assert result[0]["final_score"] > result[1]["final_score"]
        # Tie-break deterministic when equal finals.
        equal = rerank(
            "q",
            docs,
            limit=2,
            scorer=lambda _p: [0.0, 0.0],
        )
        assert [d["slug"] for d in equal] == ["a", "b"]

    def test_rerank_respects_limit(self):
        docs = [
            {
                "slug": f"p{i}",
                "candidate_id": f"p{i}",
                "title": f"Page {i}",
                "summary": "x",
                "score": 1.0,
                "rrf_score": 1.0,
            }
            for i in range(20)
        ]
        with patch("reranker._get_reranker_bundle", return_value=None):
            result = rerank("query", docs, limit=5)
        assert len(result) == 5


class TestShouldRerank:
    def test_bypass_exact_and_tiny(self):
        assert should_rerank(
            profile="HYBRID",
            candidates=[{"rrf_score": 1.0}],
        ) == (False, "tiny_result_set")
        assert should_rerank(
            profile="HYBRID",
            candidates=[{"rrf_score": 1.0}, {"rrf_score": 0.9}],
            analysis_intents=("quoted_phrase",),
        )[0] is False
        assert should_rerank(
            profile="EXACT",
            candidates=[{"rrf_score": 1.0}, {"rrf_score": 0.9}],
        )[0] is False

    def test_invoke_on_disagreement_and_global(self):
        disagree = [
            {"rrf_score": 0.5, "bm25_rank": 1, "vector_rank": 5},
            {"rrf_score": 0.4, "bm25_rank": 2, "vector_rank": 1},
        ]
        assert should_rerank(profile="HYBRID", candidates=disagree)[0] is True
        assert should_rerank(
            profile="GLOBAL",
            candidates=[{"rrf_score": 1.0}, {"rrf_score": 0.1}],
        )[0] is True
        # Profile alone is not enough without an explicit trigger.
        assert should_rerank(
            profile="HYBRID",
            candidates=[
                {"rrf_score": 1.0, "bm25_rank": 1, "vector_rank": 1},
                {"rrf_score": 0.1, "bm25_rank": 2, "vector_rank": 2},
            ],
        )[0] is False


class TestSigmoid:
    def test_sigmoid_zero(self):
        assert abs(_sigmoid(0) - 0.5) < 0.001

    def test_sigmoid_large_positive(self):
        assert abs(_sigmoid(100) - 1.0) < 0.001

    def test_sigmoid_large_negative(self):
        assert _sigmoid(-100) < 0.001

    def test_sigmoid_symmetry(self):
        for x in [0.5, 1.0, 2.0, 5.0]:
            assert abs(_sigmoid(x) + _sigmoid(-x) - 1.0) < 0.001


class TestSearchMemoryRerankerIntegration:
    def test_maybe_rerank_passthrough(self):
        from search_memory import _maybe_rerank

        docs = [{"slug": f"p{i}"} for i in range(5)]
        result = _maybe_rerank("query", docs, limit=3)
        assert len(result) == 3
