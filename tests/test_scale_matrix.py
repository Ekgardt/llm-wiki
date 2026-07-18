"""Task 28: scale and failure matrix contracts (deterministic offline smoke)."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BENCHMARK = ROOT / "benchmark"
RUNNER = BENCHMARK / "run_scale_matrix.py"
REPORT_SCHEMA = BENCHMARK / "scale-matrix-smoke-report-v1.schema.json"

CORPUS_SIZES = (1000, 5000, 20000, 50000, 100000)
SELECTIVITY = (1.0, 0.1, 0.01, 0.001)
ADAPTER_IDS = (
    "exact-numpy",
    "sqlite-vec",
    "usearch",
    "lancedb-flat",
    "lancedb-ann",
)
CRASH_POINTS = (
    "before_fsync",
    "before_activation",
    "after_activation",
)
METRIC_KEYS = {
    "latency_ms",
    "rss_bytes",
    "disk_bytes",
    "build_ms",
    "update_ms",
    "delete_ms",
    "startup_ms",
    "concurrent_reader_p95_ms",
    "recall_at_10",
    "recall_at_50",
    "batch_throughput_qps",
}


def _runner():
    spec = importlib.util.spec_from_file_location("run_scale_matrix", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_required_scale_matrix_artifacts_exist() -> None:
    assert RUNNER.is_file()
    assert REPORT_SCHEMA.is_file()


def test_closed_corpus_sizes_and_selectivity_are_exported() -> None:
    runner = _runner()
    assert runner.CORPUS_SIZES == CORPUS_SIZES
    assert runner.SELECTIVITY_FRACTIONS == SELECTIVITY
    assert runner.ADAPTER_IDS == ADAPTER_IDS
    assert runner.CRASH_POINTS == CRASH_POINTS
    assert runner.ADOPTION_RECALL_FLOOR == 0.98
    assert runner.ADOPTION_LATENCY_SPEEDUP_FLOOR == 2.0


def test_generate_corpus_is_deterministic_and_structurally_realistic() -> None:
    np = pytest.importorskip("numpy")
    runner = _runner()
    a = runner.generate_corpus(n_chunks=64, dimensions=8, seed=7)
    b = runner.generate_corpus(n_chunks=64, dimensions=8, seed=7)
    c = runner.generate_corpus(n_chunks=64, dimensions=8, seed=8)

    assert a.chunk_ids == b.chunk_ids
    assert np.array_equal(a.vectors, b.vectors)
    assert not np.array_equal(a.vectors, c.vectors)
    assert a.vectors.shape == (64, 8)
    assert a.vectors.dtype == np.dtype("float32")
    assert np.isfinite(a.vectors).all()
    # Structurally realistic: project/status/validity metadata + multi-parent pages.
    assert len(a.projects) == 64
    assert set(a.status) <= {"active", "superseded"}
    assert any(item == "superseded" for item in a.status)
    assert len(set(a.parent_ids)) < 64
    assert len(a.filters["project"]) == 64


def test_filter_selectivity_targets_are_realized() -> None:
    runner = _runner()
    corpus = runner.generate_corpus(n_chunks=1000, dimensions=4, seed=3)
    for fraction in SELECTIVITY:
        mask = runner.selectivity_mask(corpus, fraction=fraction, seed=3)
        selected = int(mask.sum())
        expected = max(1, int(round(1000 * fraction)))
        # Allow 1-slot rounding tolerance on tiny fractions.
        assert abs(selected - expected) <= 1
        assert selected >= 1


def test_exact_numpy_is_ground_truth_for_recall() -> None:
    np = pytest.importorskip("numpy")
    runner = _runner()
    corpus = runner.generate_corpus(n_chunks=48, dimensions=6, seed=11)
    query = corpus.vectors[0]
    truth = runner.exact_numpy_search(corpus.vectors, query, k=10, mask=None)
    # Self-hit is rank 0 for the source vector.
    assert truth.ids[0] == corpus.chunk_ids[0]
    # Permuting rows must not change the neighbor identity multiset for same vectors.
    perm = np.random.default_rng(0).permutation(len(corpus.vectors))
    shuffled = corpus.vectors[perm]
    shuffled_ids = [corpus.chunk_ids[i] for i in perm]
    again = runner.exact_numpy_search(shuffled, query, k=10, mask=None, ids=shuffled_ids)
    assert set(again.ids[:5]) == set(truth.ids[:5])


def test_optional_adapters_are_fail_closed_when_missing(monkeypatch) -> None:
    runner = _runner()
    corpus = runner.generate_corpus(n_chunks=16, dimensions=4, seed=1)
    for adapter_id in ("sqlite-vec", "usearch", "lancedb-flat", "lancedb-ann"):
        monkeypatch.setattr(runner, "_adapter_available", lambda _id: False)
        result = runner.run_adapter(
            adapter_id,
            corpus=corpus,
            queries=corpus.vectors[:2],
            k=5,
            mode="ann",
        )
        assert result["status"] == "skipped"
        assert result["reason"] == "dependency_unavailable"
        assert result.get("adopted") is not True
        assert "default" not in json.dumps(result).lower() or result.get("is_default") is False


def test_metrics_bundle_includes_required_operational_fields() -> None:
    runner = _runner()
    report = runner.run_smoke(corpus_size=32, dimensions=4, seed=2, queries=4)
    assert report["schema_version"] == "scale-matrix-smoke/v1"
    assert report["mode"] == "smoke"
    assert report["ground_truth"] == "exact-numpy"
    assert report["ann_default_claimed"] is False
    cell = report["cells"][0]
    for key in METRIC_KEYS:
        assert key in cell["metrics"]
    assert "cold" in cell["latency_profiles"]
    assert "warm" in cell["latency_profiles"]
    for side in ("p50_ms", "p95_ms", "p99_ms"):
        assert side in cell["latency_profiles"]["cold"]
        assert side in cell["latency_profiles"]["warm"]


def test_crash_points_are_enumerated_and_fail_closed() -> None:
    runner = _runner()
    outcomes = runner.run_crash_matrix(
        corpus_size=16,
        dimensions=4,
        seed=5,
        points=CRASH_POINTS,
    )
    assert set(outcomes) == set(CRASH_POINTS)
    for point, payload in outcomes.items():
        assert payload["injected"] is True
        assert payload["recovered_cleanly"] is True
        assert payload["partial_activation"] is False
        assert payload["status"] in {"passed", "skipped_platform"}


def test_adoption_gate_is_fail_closed() -> None:
    runner = _runner()
    # High recall but insufficient speedup → reject.
    decision = runner.evaluate_adoption_gate(
        recall_at_10=0.99,
        recall_at_50=0.99,
        exact_p95_ms=100.0,
        candidate_p95_ms=60.0,
    )
    assert decision["adopt"] is False
    assert "latency" in decision["reasons"][0]

    # Fast but low recall → reject.
    decision = runner.evaluate_adoption_gate(
        recall_at_10=0.90,
        recall_at_50=0.95,
        exact_p95_ms=100.0,
        candidate_p95_ms=10.0,
    )
    assert decision["adopt"] is False
    assert any("recall" in reason for reason in decision["reasons"])

    # Meets both floors → adopt only as measured evidence, never silent default.
    decision = runner.evaluate_adoption_gate(
        recall_at_10=0.99,
        recall_at_50=0.985,
        exact_p95_ms=100.0,
        candidate_p95_ms=40.0,
    )
    assert decision["adopt"] is True
    assert decision["becomes_default"] is False
    assert decision["requires_measurement"] is True


def test_smoke_report_validates_against_schema() -> None:
    from reliable_memory import validate_schema

    runner = _runner()
    report = runner.run_smoke(corpus_size=24, dimensions=4, seed=9, queries=3)
    validate_schema(report, REPORT_SCHEMA)
    # Full matrix sizes are declared but smoke must not expand to heavy corpora.
    assert report["declared_corpus_sizes"] == list(CORPUS_SIZES)
    assert report["executed_corpus_sizes"] == [24]
    assert report["heavy_parallel"] is False


def test_cli_smoke_json_is_deterministic(tmp_path) -> None:
    out_a = tmp_path / "a.json"
    out_b = tmp_path / "b.json"
    cmd = [
        sys.executable,
        str(RUNNER),
        "--smoke",
        "--json",
        "--seed",
        "13",
        "--corpus-size",
        "20",
        "--dimensions",
        "4",
        "--queries",
        "2",
    ]
    env = {**dict(**{k: v for k, v in __import__("os").environ.items()}), "PYTHONPATH": str(ROOT / "scripts")}
    r1 = subprocess.run(cmd + ["--output", str(out_a)], capture_output=True, text=True, env=env, check=False)
    r2 = subprocess.run(cmd + ["--output", str(out_b)], capture_output=True, text=True, env=env, check=False)
    assert r1.returncode == 0, r1.stderr
    assert r2.returncode == 0, r2.stderr
    a = json.loads(out_a.read_text(encoding="utf-8"))
    b = json.loads(out_b.read_text(encoding="utf-8"))
    # Drop wall-clock fields that may jitter slightly.
    for report in (a, b):
        report.pop("generated_at", None)
        for cell in report.get("cells", []):
            cell.get("metrics", {}).pop("rss_bytes", None)
            cell.get("metrics", {}).pop("startup_ms", None)
            cell.get("metrics", {}).pop("build_ms", None)
            cell.get("metrics", {}).pop("update_ms", None)
            cell.get("metrics", {}).pop("delete_ms", None)
            cell.get("metrics", {}).pop("concurrent_reader_p95_ms", None)
            cell.get("metrics", {}).pop("batch_throughput_qps", None)
            cell.get("metrics", {}).pop("latency_ms", None)
            if "latency_profiles" in cell:
                cell["latency_profiles"] = {
                    "cold": {"p50_ms": 0, "p95_ms": 0, "p99_ms": 0},
                    "warm": {"p50_ms": 0, "p95_ms": 0, "p99_ms": 0},
                }
    assert a == b


def test_no_ann_default_without_measured_adoption() -> None:
    runner = _runner()
    report = runner.run_smoke(corpus_size=16, dimensions=4, seed=1, queries=2)
    assert report["ann_default_claimed"] is False
    assert report["selected_default_backend"] == "exact-numpy"
    for cell in report["cells"]:
        if cell["adapter"] != "exact-numpy":
            assert cell["metrics"]["recall_at_10"] is None or cell["status"] in {
                "ok",
                "skipped",
            }
            assert cell.get("adoption", {}).get("becomes_default") is not True


def test_heavy_sizes_are_serial_only() -> None:
    runner = _runner()
    plan = runner.plan_matrix(
        corpus_sizes=CORPUS_SIZES,
        adapters=("exact-numpy", "usearch"),
        selectivity=SELECTIVITY,
        parallel=True,
    )
    assert plan["parallel_allowed"] is False
    assert plan["execution"] == "serial"
    assert plan["reason"] == "heavy_runs_must_be_serial"
