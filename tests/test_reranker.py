"""Tests for the cross-encoder reranker module (reranker.py)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, NoReturn
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import reranker as reranker_module  # noqa: E402
from reranker import (  # noqa: E402
    _sigmoid,
    rerank,
    reranker_available,
    should_rerank,
)


def _reset_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    import reranker

    monkeypatch.setattr(reranker, "_reranker_bundle", None)
    monkeypatch.setattr(reranker, "_reranker_unavailable_reason", None)


def test_no_environment_configuration_means_the_default_reranker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The product ships a reranker; an environment that names none gets it."""
    import reranker

    monkeypatch.delenv("LLMWIKI_RERANKER_MODEL", raising=False)
    monkeypatch.delenv("LLMWIKI_RERANKER_REVISION", raising=False)

    assert reranker.configured_reranker_identity() == (
        reranker.DEFAULT_RERANKER_MODEL,
        reranker.DEFAULT_RERANKER_REVISION,
    )
    assert reranker.DEFAULT_RERANKER_MODEL == "Horizon-Labs/multilingual-reranker-base"
    assert reranker.IMMUTABLE_REVISION.fullmatch(reranker.DEFAULT_RERANKER_REVISION)


def test_the_off_switch_means_no_reranker_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import reranker

    monkeypatch.setenv("LLMWIKI_RERANKER_MODEL", "off")
    monkeypatch.delenv("LLMWIKI_RERANKER_REVISION", raising=False)
    _reset_bundle(monkeypatch)
    monkeypatch.setattr(
        reranker,
        "_libraries_importable",
        lambda: pytest.fail("dependency probe ran with the reranker switched off"),
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


def _installed_model(monkeypatch: pytest.MonkeyPatch, root: Path, repo: str, revision: str) -> Path:
    """The model's two files under a temporary `cache/models/`."""
    import model_files
    import reranker

    monkeypatch.setattr(model_files, "models_root", lambda: root)
    directory = model_files.model_directory(repo, revision)
    for name in reranker.RERANKER_READ_FILES:
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(b"")
    installed: Path = directory
    return installed


def test_explicit_immutable_model_loads_from_its_pinned_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import reranker

    revision = "a" * 40
    monkeypatch.setenv("LLMWIKI_RERANKER_MODEL", "org/model")
    monkeypatch.setenv("LLMWIKI_RERANKER_REVISION", revision)
    _reset_bundle(monkeypatch)
    monkeypatch.setattr(reranker, "_libraries_importable", lambda: True)
    directory = _installed_model(monkeypatch, tmp_path, "org/model", revision)
    compiled: list[Path] = []
    tokenizers: list[Path] = []

    def compile_model(path: Path) -> str:
        compiled.append(path)
        return "m"

    def pair_tokenizer(path: Path) -> str:
        tokenizers.append(path)
        return "t"

    monkeypatch.setattr("openvino_runtime.compiled_model", compile_model)
    monkeypatch.setattr(reranker, "_pair_tokenizer", pair_tokenizer)

    bundle = reranker._get_reranker_bundle()

    assert bundle is not None
    assert (bundle["model_id"], bundle["model_revision"], bundle["model"]) == (
        "org/model",
        revision,
        "m",
    )
    assert (compiled, tokenizers) == (
        [directory / reranker.RERANKER_WEIGHTS_FILE],
        [directory / reranker.RERANKER_TOKENIZER_FILE],
    )


def test_missing_local_artifact_degrades_without_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import model_files
    import reranker

    monkeypatch.setenv("LLMWIKI_RERANKER_MODEL", "org/missing")
    monkeypatch.setenv("LLMWIKI_RERANKER_REVISION", "b" * 40)
    _reset_bundle(monkeypatch)
    monkeypatch.setattr(reranker, "_libraries_importable", lambda: True)
    monkeypatch.setattr(model_files, "models_root", lambda: tmp_path)
    monkeypatch.setattr(
        "openvino_runtime.compiled_model", lambda path: pytest.fail("compiled a missing model")
    )

    assert reranker._get_reranker_bundle() is None
    assert "is not installed" in reranker.reranker_unavailable_reason()


def test_a_failed_load_is_recorded_once_and_not_retried_per_question(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A default reranker without local weights must not cost every question a load."""
    import reranker

    monkeypatch.setenv("LLMWIKI_RERANKER_MODEL", "org/broken")
    monkeypatch.setenv("LLMWIKI_RERANKER_REVISION", "c" * 40)
    _reset_bundle(monkeypatch)
    monkeypatch.setattr(reranker, "_libraries_importable", lambda: True)
    _installed_model(monkeypatch, tmp_path, "org/broken", "c" * 40)
    calls: list[Path] = []

    def broken(path: Path) -> NoReturn:
        calls.append(path)
        raise RuntimeError("cannot compile")

    monkeypatch.setattr("openvino_runtime.compiled_model", broken)
    monkeypatch.setattr(reranker, "_pair_tokenizer", lambda path: "t")

    assert reranker._get_reranker_bundle() is None
    assert reranker._get_reranker_bundle() is None
    assert len(calls) == 1
    assert reranker.reranker_unavailable_reason() == "RuntimeError: cannot compile"


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

        first, second = rerank("query", docs, limit=10, scorer=fake_scorer)
        assert (first["slug"], first["reranker_applied"], first["reranker_model_id"]) == (
            "high",
            True,
            "fake-cross-encoder",
        )
        assert first["final_score"] >= second["final_score"]
        assert first["score"] == first["final_score"]

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
        # Prefix of 3 was reranked; remaining 5 keep original order after prefix.
        tail_slugs = [d["slug"] for d in result[3:]]
        assert (len(result), tail_slugs) == (8, ["p3", "p4", "p5", "p6", "p7"])
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

    def test_every_question_in_a_rerank_profile_is_reranked(self, monkeypatch):
        """No trigger is needed: a Russian question over English pages matched
        none of the old ones, and that is the question the reranker is for."""
        # The reranker is installed here; its absence is its own refusal (audit 2026-09-26 B-16).
        monkeypatch.setattr("reranker.reranker_installed", lambda: True)
        agreeing = [
            {"rrf_score": 1.0, "bm25_rank": 1, "vector_rank": 1},
            {"rrf_score": 0.1, "bm25_rank": 2, "vector_rank": 2},
        ]
        assert should_rerank(profile="HYBRID", candidates=agreeing) == (True, None)
        assert should_rerank(profile="BASE", candidates=agreeing) == (True, None)
        assert should_rerank(profile="GLOBAL", candidates=agreeing) == (True, None)
        assert should_rerank(
            profile="HYBRID", candidates=agreeing, rerank_enabled=False
        ) == (False, "rerank_disabled")

    def test_the_default_depth_is_ten(self):
        assert reranker_module.DEFAULT_RERANK_DEPTH == 10


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


def _word_tokenizer_file(tmp_path: Path, *, shipped_window: int | None) -> Path:
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace

    vocabulary = {"<pad>": 0, "<unk>": 1, "a": 2, "b": 3}
    tokenizer = Tokenizer(WordLevel(vocabulary, unk_token="<unk>"))
    tokenizer.pre_tokenizer = Whitespace()
    if shipped_window is not None:
        tokenizer.enable_truncation(max_length=shipped_window, strategy="only_second")
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    return path


def test_the_tokenizer_keeps_the_window_the_model_ships_with(tmp_path: Path) -> None:
    pytest.importorskip("tokenizers")
    import reranker

    shipped = reranker._pair_tokenizer(_word_tokenizer_file(tmp_path, shipped_window=3))

    truncation = shipped.truncation
    assert (truncation["max_length"], truncation["strategy"]) == (3, "longest_first")
    assert shipped.padding is not None


def test_a_query_longer_than_the_window_is_still_scored(tmp_path: Path) -> None:
    pytest.importorskip("tokenizers")
    import numpy as np
    import reranker

    tokenizer = reranker._pair_tokenizer(_word_tokenizer_file(tmp_path, shipped_window=3))

    def model(feeds: dict[str, Any]) -> list[Any]:
        return [feeds["attention_mask"].sum(axis=1, keepdims=True).astype(np.float32)]

    bundle = {"model": model, "tokenizer": tokenizer}

    assert reranker._batch_logits(bundle, [("a b a b a", "b")]) == [3.0]


def test_a_tokenizer_without_a_window_gets_the_product_one(tmp_path: Path) -> None:
    pytest.importorskip("tokenizers")
    import reranker

    bare = reranker._pair_tokenizer(_word_tokenizer_file(tmp_path, shipped_window=None))

    assert bare.truncation["max_length"] == reranker.RERANK_MAX_TOKENS
    assert bare.padding["pad_id"] == 0


def test_a_batch_feeds_ids_and_mask_and_reads_one_logit_per_pair(tmp_path: Path) -> None:
    pytest.importorskip("tokenizers")
    import numpy as np
    import reranker

    tokenizer = reranker._pair_tokenizer(_word_tokenizer_file(tmp_path, shipped_window=None))
    fed: list[dict[str, Any]] = []

    def model(feeds: dict[str, Any]) -> list[Any]:
        fed.append(feeds)
        return [feeds["attention_mask"].sum(axis=1, keepdims=True).astype(np.float32)]

    bundle = {"model": model, "tokenizer": tokenizer}
    logits = reranker._batch_logits(bundle, [("a", "b"), ("a", "a b a")])

    assert logits == [2.0, 4.0]
    assert fed[0]["input_ids"].tolist() == [[2, 3, 0, 0], [2, 2, 3, 2]]
    assert fed[0]["input_ids"].dtype == np.int64
