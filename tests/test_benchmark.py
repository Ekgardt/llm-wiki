from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


def _benchmark_module():
    path = Path(__file__).resolve().parent.parent / "benchmark" / "run_benchmark.py"
    spec = importlib.util.spec_from_file_location("run_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_legacy_corpus_is_versioned_and_exactly_sixty_queries():
    benchmark = _benchmark_module()

    corpus = benchmark._load_legacy_corpus()

    assert corpus["version"] == "legacy-60-v1"
    assert len(corpus["queries"]) == 60


def test_legacy_corpus_queries_are_loaded_verbatim(monkeypatch):
    benchmark = _benchmark_module()
    monkeypatch.setattr(
        benchmark,
        "_generate_qa_pairs",
        lambda: (_ for _ in ()).throw(AssertionError("legacy corpus was regenerated")),
    )

    corpus = benchmark._load_legacy_corpus()

    assert corpus["queries"][0] == {
        "query": "three conventions, one root — 2026-04-13 memory review",
        "gold_path": "knowledge/notes/2026-04-13 Three Conventions One Root.md",
        "query_type": "exact_title",
    }
    assert corpus["queries"][-1]["query"] == "three approaches giving durable"


def test_search_runtime_isolates_indexes_vectors_lancedb_and_model_caches(
    tmp_path, monkeypatch
):
    benchmark = _benchmark_module()
    installed = tmp_path / "installed-vault"
    runtime = tmp_path / "benchmark-runtime"
    installed.mkdir()
    sentinel = installed / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    monkeypatch.setenv("LLM_WIKI_ROOT", str(installed))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(installed))

    with benchmark._isolated_search_runtime(runtime) as search_memory:
        import lance_store

        runtime_paths = [
            search_memory.INDEX_DIR,
            search_memory.INDEX_FILE,
            search_memory.INDEX_MANIFEST,
            search_memory.VECTOR_NPY,
            search_memory.VECTOR_META,
            lance_store.LANCEDB_DIR,
        ]
        assert all(path.is_relative_to(runtime) for path in runtime_paths)
        assert search_memory.ROOT == benchmark.ROOT
        assert search_memory.KNOWLEDGE_DIR == benchmark.KNOWLEDGE
        for name in benchmark.MODEL_CACHE_ENV:
            assert Path(os.environ[name]).is_relative_to(runtime)

    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert list(installed.iterdir()) == [sentinel]


def test_legacy_only_command_returns_nonzero_below_gate(monkeypatch):
    benchmark = _benchmark_module()
    monkeypatch.setattr(sys, "argv", ["run_benchmark.py", "--legacy-only"])
    monkeypatch.setattr(
        benchmark,
        "_load_legacy_corpus",
        lambda: {"version": "legacy-60-v1", "queries": [{"query": "q"}]},
    )
    monkeypatch.setattr(benchmark, "_tracked_knowledge_paths", lambda: [])
    monkeypatch.setattr(
        benchmark,
        "_run_benchmark",
        lambda *args, **kwargs: {
            "recall_at_k": {5: 0.99},
            "corpus_version": "legacy-60-v1",
        },
    )

    assert benchmark.main() == 2


def test_report_lists_queries_missed_at_recall_five():
    benchmark = _benchmark_module()
    results = {
        "semantic": False,
        "total_queries": 2,
        "corpus_version": "test-v1",
        "k_values": [1, 5],
        "recall_at_k": {1: 0.0, 5: 0.5},
        "mrr": 0.1,
        "latency_p50_ms": 1.0,
        "latency_p95_ms": 2.0,
        "latency_avg_ms": 1.5,
        "per_query": [
            {"query": "late", "query_type": "exact_title", "gold": "late.md", "found_at": 6},
            {"query": "good", "query_type": "exact_title", "gold": "good.md", "found_at": 2},
        ],
    }

    report = benchmark._format_report(results)

    assert "Missed at Recall@5" in report
    assert "rank 6" in report


def test_regression_gate_rejects_hidden_current_or_legacy_drop():
    benchmark = _benchmark_module()

    assert benchmark._passes_regression_gates(
        {"recall_at_k": {5: 0.99}}, {"recall_at_k": {5: 1.0}}
    )
    assert not benchmark._passes_regression_gates(
        {"recall_at_k": {5: 0.94}}, {"recall_at_k": {5: 1.0}}
    )
    assert not benchmark._passes_regression_gates(
        {"recall_at_k": {5: 0.99}}, {"recall_at_k": {5: 0.99}}
    )
