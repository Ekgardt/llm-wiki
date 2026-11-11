"""Injected real retrieval v2 runs report provenance, clean up and degrade to lexical."""
from __future__ import annotations

import json
import os
import secrets
import sys
import weakref
from pathlib import Path

import pytest

from tests.retrieval_v2_helpers import CORPUS, SCHEMA

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import retrieval_v2_corpus  # noqa: E402
import retrieval_v2_environment  # noqa: E402
import retrieval_v2_run  # noqa: E402
import retrieval_v2_transformers  # noqa: E402


def test_injected_real_run_reports_provenance_but_never_quality_or_release(tmp_path):
    np = pytest.importorskip("numpy")
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    loader_calls = []

    def loader(selection, **options):
        loader_calls.append((selection.embedding["id"], options, dict(os.environ)))

        def encode(texts, **encode_options):
            del encode_options
            vectors = np.zeros((len(texts), 384), dtype=np.float32)
            for row, text in enumerate(texts):
                vectors[row, hash(text) % 384] = 1.0
            return vectors

        return encode

    output = tmp_path / "report.json"
    report = retrieval_v2_run.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=tmp_path / "cache",
        adapter="model-matrix",
        matrix_path=BENCHMARK / "model-matrix-v1.json",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        lexical_config="L0",
        vector_backend="numpy-exact",
        model_loader=loader,
        raw_output_written=True,
    )

    assert loader_calls[0][1] == {
        "cache_root": (tmp_path / "cache").resolve(),
        "local_files_only": True,
        "trust_remote_code": False,
    }
    assert loader_calls[0][2]["HF_HUB_OFFLINE"] == "1"
    assert report["adapter_kind"] == "model-matrix"
    assert report["model_id"] == "BAAI/bge-small-en-v1.5"
    assert report["variant_id"] == "float32-384d"
    assert report["revision"] == "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
    assert report["matrix_sha256"] == retrieval_v2_environment.sha256_file(BENCHMARK / "model-matrix-v1.json")
    assert report["corpus_sha256"] == retrieval_v2_environment.sha256_file(CORPUS)
    assert report["acquisition_mode"] == "offline-local-files-only"
    assert report["vector_backend"] == "numpy-exact"
    assert report["quality_claim"] is False
    assert report["release_evidence"] is False
    assert report["gates"]["release_evidence"] is False
    assert all("pre_rerank_candidate_ids" in trace for trace in report["traces"])
    assert not output.exists()


def test_single_provenance_valid_run_is_quality_candidate_never_release(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)

    def load_encoder(*args, **kwargs):
        del args, kwargs

        def encode(texts, **options):
            del options
            vectors = np.ones((len(texts), 384), dtype=np.float32)
            vectors[:, 1] = np.arange(1, len(texts) + 1)
            return vectors

        return encode

    monkeypatch.setattr(retrieval_v2_transformers, "load_transformer_embedding", load_encoder)
    report = retrieval_v2_run.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=tmp_path / "cache",
        adapter="model-matrix",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        lexical_config="L0",
        vector_backend="numpy-exact",
        raw_output_written=True,
    )

    assert report["quality_claim"] is False
    assert report["release_evidence"] is False
    assert report["gates"]["release_evidence"] is False
    assert report["requested_mode"] == "model-matrix"
    assert report["effective_mode"] == "model-matrix"
    assert report["fallback_reason"] is None

    stdout_only = retrieval_v2_run.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=tmp_path / "stdout-cache",
        adapter="model-matrix",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        lexical_config="L0",
        vector_backend="numpy-exact",
        raw_output_written=False,
    )
    assert stdout_only["quality_claim"] is False


def test_two_real_runs_reuse_model_cache_with_unique_cleaned_workspaces(tmp_path):
    np = pytest.importorskip("numpy")
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    cache = tmp_path / "persistent-model-cache"
    cache.mkdir()
    model_sentinel = cache / "model-sentinel.bin"
    model_sentinel.write_bytes(b"model")

    def encoder(texts, **options):
        del options
        return np.ones((len(texts), 1024), dtype=np.float32)

    reports = [
        retrieval_v2_run.run_benchmark(
            corpus,
            corpus_path=CORPUS,
            cache_root=cache,
            adapter="model-matrix",
            model_id="BAAI/bge-m3",
            variant_id="float32-1024d",
            lexical_config="L0",
            vector_backend="numpy-exact",
            encoder=encoder,
        )
        for _ in range(2)
    ]

    assert all(report["effective_mode"] == "model-matrix" for report in reports)
    assert model_sentinel.read_bytes() == b"model"
    assert not (cache / "lexical-L0.sqlite3").exists()
    assert (cache / "runs").is_dir()
    assert not list((cache / "runs").iterdir())
    isolation = [report["methodology"]["cache_isolation"] for report in reports]
    assert isolation[0] == isolation[1]
    assert "nonce" not in json.dumps(isolation).casefold()


def test_bge_m3_report_separates_dense_and_learned_sparse_traces_and_bytes(tmp_path):
    np = pytest.importorskip("numpy")
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)

    def encoder(texts, **options):
        del options
        dense = np.zeros((len(texts), 1024), dtype=np.float32)
        dense[:, 0] = 1.0
        return {
            "dense_vecs": dense,
            "lexical_weights": [{str(index + 10): 0.5} for index, _text in enumerate(texts)],
        }

    report = retrieval_v2_run.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=tmp_path / "cache",
        adapter="model-matrix",
        model_id="BAAI/bge-m3",
        variant_id="float32-1024d",
        lexical_config="L0",
        vector_backend="numpy-exact",
        encoder=encoder,
    )

    assert report["effective_mode"] == "model-matrix"
    assert report["measurements"]["learned_sparse_bytes"] > 0
    assert report["measurements"]["measurement_status"]["learned_sparse_bytes"] == "measured"
    assert all(
        set(trace["embedding_signals"]) == {
            "dense_candidate_ids",
            "fusion",
            "learned_sparse_candidate_ids",
        }
        for trace in report["traces"]
    )


def test_real_run_failure_cleans_workspace_and_retains_model_cache(tmp_path):
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    cache = tmp_path / "persistent-model-cache"
    cache.mkdir()
    sentinel = cache / "model-sentinel"
    sentinel.write_text("keep", encoding="utf-8")

    def interrupted(*args, **kwargs):
        del args, kwargs
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        retrieval_v2_run.run_benchmark(
            corpus,
            corpus_path=CORPUS,
            cache_root=cache,
            adapter="model-matrix",
            model_id="BAAI/bge-small-en-v1.5",
            variant_id="float32-384d",
            lexical_config="L0",
            vector_backend="numpy-exact",
            model_loader=interrupted,
        )

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not list((cache / "runs").iterdir())


def test_real_run_ignores_stale_workspace_and_rejects_symlinked_runs(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    cache = tmp_path / "cache"
    stale = cache / "runs" / ("0" * 32)
    stale.mkdir(parents=True)
    os.chmod(cache / "runs", 0o700)
    stale_sentinel = stale / "stale"
    stale_sentinel.write_text("untouched", encoding="utf-8")
    tokens = iter(("0" * 32, "1" * 32))
    monkeypatch.setattr(secrets, "token_hex", lambda _size: next(tokens))
    monkeypatch_target = tmp_path / "outside"
    monkeypatch_target.mkdir()

    report = retrieval_v2_run.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=cache,
        adapter="model-matrix",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        lexical_config="L0",
        vector_backend="numpy-exact",
        encoder=lambda texts, **options: np.ones((len(texts), 384), dtype=np.float32),
    )
    assert report["effective_mode"] == "model-matrix"
    assert stale_sentinel.read_text(encoding="utf-8") == "untouched"
    assert list((cache / "runs").iterdir()) == [stale]

    second_cache = tmp_path / "symlink-cache"
    second_cache.mkdir()
    try:
        (second_cache / "runs").symlink_to(monkeypatch_target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(ValueError, match="runs.*symlink|reparse"):
        retrieval_v2_run.run_benchmark(
            corpus,
            corpus_path=CORPUS,
            cache_root=second_cache,
            adapter="model-matrix",
            model_id="BAAI/bge-small-en-v1.5",
            variant_id="float32-384d",
            lexical_config="L0",
            vector_backend="numpy-exact",
            encoder=lambda texts, **options: np.ones((len(texts), 384), dtype=np.float32),
        )
    assert not list(monkeypatch_target.iterdir())


def test_online_acquisition_can_never_be_quality_evidence(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)

    def load_encoder(*args, **kwargs):
        del args, kwargs
        return lambda texts, **options: np.ones((len(texts), 384), dtype=np.float32)

    monkeypatch.setattr(retrieval_v2_transformers, "load_transformer_embedding", load_encoder)
    report = retrieval_v2_run.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=tmp_path / "cache",
        adapter="model-matrix",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        lexical_config="L0",
        vector_backend="numpy-exact",
        allow_download=True,
        raw_output_written=True,
    )

    assert report["acquisition_mode"] == "download-allowed-explicit-cache"
    assert report["quality_claim"] is False


def test_forged_worker_environment_and_argument_never_self_attest(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    monkeypatch.setenv("LLM_WIKI_BENCHMARK_WORKER", "forged")
    monkeypatch.setattr(
        retrieval_v2_transformers,
        "load_transformer_embedding",
        lambda *args, **kwargs: lambda texts, **options: np.ones(
            (len(texts), 384), dtype=np.float32
        ),
    )

    report = retrieval_v2_run.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=tmp_path / "cache",
        adapter="model-matrix",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        lexical_config="L0",
        vector_backend="numpy-exact",
        raw_output_written=True,
    )

    assert report["quality_claim"] is False
    assert report["release_evidence"] is False


def test_injected_clock_separates_lexical_model_and_document_phases(tmp_path):
    np = pytest.importorskip("numpy")
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    ticks = iter((0.0, 1.0, 10.0, 12.0, 20.0, 24.0))

    report = retrieval_v2_run.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=tmp_path / "cache",
        adapter="model-matrix",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        lexical_config="L0",
        vector_backend="numpy-exact",
        encoder=lambda texts, **options: np.ones((len(texts), 384), dtype=np.float32),
        clock=lambda: next(ticks),
    )

    assert report["measurements"]["lexical_build_ms"] == 1_000.0
    assert report["measurements"]["model_load_ms"] == 2_000.0
    assert report["measurements"]["document_encoding_index_build_ms"] == 4_000.0
    assert report["measurements"]["indexing_throughput_chunks_per_second"] == (
        len(retrieval_v2_corpus.build_candidates(corpus)) / 4.0
    )


def test_model_load_failure_degrades_to_lexical_and_removes_semantic_artifacts(tmp_path):
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    cache = tmp_path / "cache"

    def unavailable(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("model unavailable")

    report = retrieval_v2_run.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=cache,
        adapter="model-matrix",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        lexical_config="L0",
        vector_backend="numpy-exact",
        model_loader=unavailable,
    )

    assert report["requested_mode"] == "model-matrix"
    assert report["effective_mode"] == "lexical-L0"
    assert report["fallback_reason"] == "RuntimeError: model unavailable"
    assert report["quality_claim"] is False
    assert report["release_evidence"] is False
    assert report["gates"]["degraded"] is True
    assert not (cache / "lexical-L0.sqlite3").exists()
    assert not list((cache / "runs").iterdir())


@pytest.mark.parametrize(
    "failure",
    [
        TypeError("bad model type"),
        AssertionError("model assertion"),
        MemoryError("model memory"),
    ],
)
def test_every_ordinary_model_failure_degrades_to_lexical(tmp_path, failure):
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)

    def unavailable(*args, **kwargs):
        del args, kwargs
        raise failure

    report = retrieval_v2_run.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=tmp_path / "cache",
        adapter="model-matrix",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        lexical_config="L0",
        vector_backend="numpy-exact",
        model_loader=unavailable,
    )

    assert report["effective_mode"] == "lexical-L0"
    assert report["fallback_reason"] == f"{type(failure).__name__}: {failure}"
    assert report["gates"]["degraded"] is True


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), SystemExit(7)])
def test_control_flow_model_exceptions_propagate_after_cleanup(tmp_path, failure):
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    cache = tmp_path / "cache"
    before = dict(os.environ)

    def interrupted(*args, **kwargs):
        del args, kwargs
        raise failure

    with pytest.raises(type(failure)):
        retrieval_v2_run.run_benchmark(
            corpus,
            corpus_path=CORPUS,
            cache_root=cache,
            adapter="model-matrix",
            model_id="BAAI/bge-small-en-v1.5",
            variant_id="float32-384d",
            lexical_config="L0",
            vector_backend="numpy-exact",
            model_loader=interrupted,
        )

    assert dict(os.environ) == before
    assert not (cache / "lexical-L0.sqlite3").exists()
    assert not list((cache / "runs").iterdir())


def test_real_reranker_report_contains_all_depth_metrics_from_same_fingerprint(tmp_path):
    np = pytest.importorskip("numpy")
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)

    def encoder(texts, **options):
        del options
        vectors = np.ones((len(texts), 384), dtype=np.float32)
        vectors[:, 0] = np.arange(1, len(texts) + 1)
        return vectors

    def scorer(inputs, **options):
        del options
        return list(range(len(inputs)))

    report = retrieval_v2_run.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=tmp_path / "cache",
        adapter="model-matrix",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        reranker_id="BAAI/bge-reranker-v2-m3",
        lexical_config="L0",
        vector_backend="numpy-exact",
        encoder=encoder,
        scorer=scorer,
    )

    assert set(report["reranker"]["depth_metrics"]) == {"10", "20", "50"}
    assert all(
        set(report["reranker"]["depth_metrics"][depth])
        == {
            "overall",
            "slices",
            "duration_ms",
            "inference_latencies_ms",
            "warm_latency_p95_ms",
            "shared_resources",
        }
        for depth in ("10", "20", "50")
    )
    for trace in report["traces"]:
        reranker_trace = trace["reranker"]
        assert set(reranker_trace["depths"]) == {"10", "20", "50"}
        assert all(
            reranker_trace["depths"][depth]["candidate_ids"]
            == reranker_trace["pre_rerank_candidate_ids"][: int(depth)]
            for depth in ("10", "20", "50")
        )


def test_embedding_is_collected_before_reranker_load_and_frozen_lists_survive(tmp_path):
    np = pytest.importorskip("numpy")
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    encoder_reference = [None]
    destroyed = []

    class Encoder:
        def __call__(self, texts, **options):
            del options
            vectors = np.ones((len(texts), 384), dtype=np.float32)
            vectors[:, 0] = np.arange(1, len(texts) + 1)
            return vectors

        def __del__(self):
            destroyed.append(True)

    def model_loader(*args, **kwargs):
        del args, kwargs
        loaded = Encoder()
        encoder_reference[0] = weakref.ref(loaded)
        return loaded

    def reranker_loader(*args, **kwargs):
        del args, kwargs
        assert encoder_reference[0]() is None
        assert destroyed == [True]
        return lambda inputs, **options: list(range(len(inputs)))

    report = retrieval_v2_run.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=tmp_path / "cache",
        adapter="model-matrix",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        reranker_id="Qwen/Qwen3-Reranker-0.6B",
        lexical_config="L0",
        vector_backend="numpy-exact",
        model_loader=model_loader,
        reranker_loader=reranker_loader,
    )

    assert report["effective_mode"] == "model-matrix"
    assert report["methodology"]["phase_peak_rss"] == (
        "process peak RSS is run-level; per-phase peaks are unavailable"
    )
    assert all(
        trace["pre_rerank_candidate_ids"]
        == trace["reranker"]["pre_rerank_candidate_ids"]
        for trace in report["traces"]
    )


def test_embedding_cache_cleanup_failure_is_not_suppressed(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)

    class Cuda:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def empty_cache():
            raise RuntimeError("CUDA cache cleanup failed")

    class Torch:
        cuda = Cuda()

    monkeypatch.setitem(sys.modules, "torch", Torch())

    with pytest.raises(RuntimeError, match="CUDA cache cleanup failed"):
        retrieval_v2_run.run_benchmark(
            corpus,
            corpus_path=CORPUS,
            cache_root=tmp_path / "cache",
            adapter="model-matrix",
            model_id="BAAI/bge-small-en-v1.5",
            variant_id="float32-384d",
            lexical_config="L0",
            vector_backend="numpy-exact",
            encoder=lambda texts, **options: np.ones(
                (len(texts), 384), dtype=np.float32
            ),
        )


def test_reranker_scoring_failure_degrades_whole_run_to_lexical(tmp_path):
    np = pytest.importorskip("numpy")
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)

    def encoder(texts, **options):
        del options
        return np.ones((len(texts), 384), dtype=np.float32)

    def scorer(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("reranker encode failed")

    report = retrieval_v2_run.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=tmp_path / "cache",
        adapter="model-matrix",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        reranker_id="BAAI/bge-reranker-v2-m3",
        lexical_config="L0",
        vector_backend="numpy-exact",
        encoder=encoder,
        scorer=scorer,
    )

    assert report["effective_mode"] == "lexical-L0"
    assert report["fallback_reason"] == "RuntimeError: reranker encode failed"
    assert report["reranker"] is None
    assert report["quality_claim"] is False
    assert report["gates"]["degraded"] is True
