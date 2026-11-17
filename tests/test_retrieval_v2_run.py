"""A fake retrieval v2 run is deterministic, complete and confined to its isolated cache."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tests.retrieval_v2_helpers import CORPUS, ROOT, SCHEMA

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import retrieval_v2_contract  # noqa: E402
import retrieval_v2_corpus  # noqa: E402
import retrieval_v2_run  # noqa: E402


def _effectiveness(report: dict) -> dict:
    copy = json.loads(json.dumps(report))
    copy["measurements"] = {
        "build_time_ms": "measured",
        "latency_p50_ms": "measured",
        "latency_p95_ms": "measured",
        "peak_rss_bytes": "measured-or-unavailable",
    }
    for trace in copy["traces"]:
        trace.pop("latency_ms", None)
    return copy


def test_prompt_injection_is_inert_untrusted_candidate_data(tmp_path):
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    injection = next(
        candidate
        for candidate in retrieval_v2_corpus.build_candidates(corpus)
        if "ignore previous instructions" in candidate.text.lower()
    )
    before = dict(os.environ)
    report = retrieval_v2_run.run_benchmark(corpus, cache_root=tmp_path / "cache")

    assert injection.evidence_id in {
        item["evidence_id"] for trace in report["traces"] for item in trace["ranked_evidence"]
    }
    assert dict(os.environ) == before
    assert set(report) == retrieval_v2_contract.REPORT_FIELDS
    assert report["adapter_kind"] == "deterministic-fake"
    assert report["quality_claim"] is False


def test_fake_run_is_deterministic_except_measured_resources(tmp_path):
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    first = retrieval_v2_run.run_benchmark(corpus, cache_root=tmp_path / "one")
    second = retrieval_v2_run.run_benchmark(corpus, cache_root=tmp_path / "two")

    assert _effectiveness(first) == _effectiveness(second)


def test_report_has_every_metric_slice_gate_and_trace(tmp_path):
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    report = retrieval_v2_run.run_benchmark(corpus, cache_root=tmp_path / "cache")
    required_metrics = {
        "evidence_recall_at_10",
        "evidence_recall_at_20",
        "evidence_recall_at_50",
        "all_required_evidence_recall_at_20",
        "parent_recall_at_10",
        "ndcg_at_10",
        "mrr_at_10",
        "no_answer_false_answer_rate",
    }
    assert required_metrics <= report["overall"].keys()
    assert set(report["slices"]) == {"EN", "RU", "ZH", "cross-language"}
    assert set(report["macro_average"]) == required_metrics
    assert report["thresholds"] == {
        "parent_recall_at_10": 0.95,
        "all_required_evidence_recall_at_20": 0.85,
        "ndcg_at_10": 0.80,
        "mrr_at_10": 0.85,
        "max_language_gate_gap": 0.03,
        "no_answer_false_answer_rate": 0.03,
    }
    assert report["gates"]["release_evidence"] is False
    assert report["gates"]["interpretation"] == "orchestration-only"
    assert report["gates"]["passed_for_orchestration"] is True
    assert report["gates"]["qa_contract_passed"] is True
    assert report["overall"]["no_answer_false_answer_rate"] == 0.0
    assert report["slices"]["cross-language"]["no_answer_false_answer_rate"] is None
    assert report["macro_average"]["no_answer_false_answer_rate"] == 0.0
    assert "represented as JSON null" in report["methodology"]["unavailable_metrics"]
    json.dumps(report, allow_nan=False)
    assert all(trace["abstention_contract_valid"] for trace in report["traces"])
    assert len(report["traces"]) == len(corpus["queries"])
    assert {trace["query_id"] for trace in report["traces"]} == {
        query["query_id"] for query in corpus["queries"]
    }
    assert set(report["measurements"]) == {
        "latency_p50_ms",
        "latency_p95_ms",
        "cold_first_query_latency_ms",
        "warm_latency_p50_ms",
        "warm_latency_p95_ms",
        "peak_rss_bytes",
        "peak_rss_status",
            "build_time_ms",
            "lexical_build_ms",
        "indexing_throughput_chunks_per_second",
        "index_size_bytes",
        "measurement_status",
    }
    assert report["measurements"]["index_size_bytes"] == (
        tmp_path / "cache" / "fake-index.json"
    ).stat().st_size


def _cache_listing(cache: Path) -> list[str] | None:
    if not cache.exists():
        return None
    return sorted(path.relative_to(cache).as_posix() for path in cache.rglob("*"))


def test_cache_is_isolated_and_no_network_or_model_access(tmp_path, monkeypatch):
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    source_cache = ROOT / "cache"
    source_cache_before = _cache_listing(source_cache)
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setattr(
        retrieval_v2_contract,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network accessed")),
    )

    isolated = tmp_path / "isolated"
    retrieval_v2_run.run_benchmark(corpus, cache_root=isolated)
    assert (isolated / "fake-index.json").is_file()
    source_cache_after = _cache_listing(source_cache)
    assert source_cache_after == source_cache_before
    with pytest.raises(ValueError):
        retrieval_v2_run.run_benchmark(corpus, cache_root=ROOT / "cache")
    with pytest.raises(ValueError):
        retrieval_v2_run.run_benchmark(corpus, cache_root=vault / "cache")
    with pytest.raises(ValueError, match="only deterministic-fake"):
        retrieval_v2_run.run_benchmark(corpus, cache_root=tmp_path / "real", adapter="real")


def test_cache_index_replaces_symlink_without_following_it(tmp_path):
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    cache = tmp_path / "cache"
    cache.mkdir()
    protected = tmp_path / "protected.txt"
    protected.write_text("unchanged", encoding="utf-8")
    link = cache / "fake-index.json"
    try:
        link.symlink_to(protected)
    except OSError:
        pytest.skip("symlink creation unavailable")

    retrieval_v2_run.run_benchmark(corpus, cache_root=cache)

    assert protected.read_text(encoding="utf-8") == "unchanged"
    assert link.is_file()
    assert not link.is_symlink()


def test_cache_publication_fails_closed_if_root_path_is_replaced(tmp_path, monkeypatch):
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    cache = tmp_path / "cache"
    moved = tmp_path / "moved-cache"
    original_replace = os.replace

    def replace_then_swap(source, destination, *args, **kwargs):
        result = original_replace(source, destination, *args, **kwargs)
        cache.rename(moved)
        cache.mkdir()
        (cache / "fake-index.json").write_text("replacement-directory", encoding="utf-8")
        return result

    monkeypatch.setattr(os, "replace", replace_then_swap)

    with pytest.raises(PermissionError, match="cache root changed"):
        retrieval_v2_run.run_benchmark(corpus, cache_root=cache)
    assert (cache / "fake-index.json").read_text(encoding="utf-8") == "replacement-directory"
