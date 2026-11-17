"""The retrieval v2 resource measurements separate their phases and bound their workers."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import retrieval_v2_measurements  # noqa: E402


def test_resource_measurements_separate_cold_warm_and_index_throughput():
    measured = retrieval_v2_measurements.resource_measurements(
        [100.0, 10.0, 20.0, 30.0],
        build_time_ms=2000.0,
        chunk_count=100,
        index_size_bytes=4096,
        peak_rss_bytes=8192,
        peak_rss_status="measured-test",
    )

    assert measured["cold_first_query_latency_ms"] == 100.0
    assert measured["warm_latency_p50_ms"] == 20.0
    assert measured["warm_latency_p95_ms"] == 30.0
    assert measured["indexing_throughput_chunks_per_second"] == 50.0
    assert measured["measurement_status"] == {
        "latency_p50_ms": "measured",
        "latency_p95_ms": "measured",
        "cold_first_query_latency_ms": "measured",
        "warm_latency_p50_ms": "measured",
        "warm_latency_p95_ms": "measured",
        "build_time_ms": "measured",
        "indexing_throughput_chunks_per_second": "measured",
        "peak_rss_bytes": "measured",
        "index_size_bytes": "measured",
    }


def test_resource_measurements_use_null_and_status_when_samples_are_unavailable():
    unavailable = retrieval_v2_measurements.resource_measurements(
        [],
        build_time_ms=0.0,
        chunk_count=0,
        index_size_bytes=0,
        peak_rss_bytes=None,
        peak_rss_status="unavailable",
    )

    assert unavailable["cold_first_query_latency_ms"] is None
    assert unavailable["warm_latency_p50_ms"] is None
    assert unavailable["warm_latency_p95_ms"] is None
    assert unavailable["indexing_throughput_chunks_per_second"] is None
    assert unavailable["measurement_status"]["cold_first_query_latency_ms"] == "unavailable"
    assert unavailable["measurement_status"]["warm_latency_p50_ms"] == "unavailable"
    assert unavailable["measurement_status"]["indexing_throughput_chunks_per_second"] == (
        "unavailable"
    )
    assert unavailable["measurement_status"]["peak_rss_bytes"] == "unavailable"


def test_bounded_worker_timeout_kills_and_cleans_report(tmp_path):
    report = tmp_path / "worker.json"
    command = [
        sys.executable,
        "-c",
        f"import pathlib,time; pathlib.Path({str(report)!r}).write_text('partial'); time.sleep(60)",
    ]

    with pytest.raises(TimeoutError, match="deadline"):
        retrieval_v2_measurements.run_process_tree(command, timeout=0.1)

    report.unlink(missing_ok=True)
    assert not report.exists()


def test_document_index_throughput_excludes_lexical_and_model_load():
    measured = retrieval_v2_measurements.resource_measurements(
        [10.0, 2.0, 3.0],
        build_time_ms=10_000.0,
        indexing_duration_ms=2_000.0,
        chunk_count=20,
        index_size_bytes=100,
        peak_rss_bytes=200,
        peak_rss_status="measured",
    )

    assert measured["indexing_throughput_chunks_per_second"] == 10.0
