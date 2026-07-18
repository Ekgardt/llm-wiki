"""Task 28: scale and failure matrix contracts (deterministic offline smoke)."""

from __future__ import annotations

import importlib.util
import json
import math
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BENCHMARK = ROOT / "benchmark"
RUNNER = BENCHMARK / "run_scale_matrix.py"
REPORT_SCHEMA = BENCHMARK / "scale-matrix-smoke-report-v1.schema.json"
FULL_REPORT_SCHEMA = BENCHMARK / "scale-matrix-full-report-v1.schema.json"

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


def _fake_full_cell(adapter: str, size: int, fraction: float) -> dict:
    """Internally consistent schema fixture; it does not claim optional measurements."""
    exact = adapter == "exact-numpy"
    metrics = {key: None for key in METRIC_KEYS}
    if exact:
        metrics.update(
            {
                "latency_ms": 1.0,
                "build_ms": 1.0,
                "recall_at_10": 1.0,
                "recall_at_50": 1.0,
                "batch_throughput_qps": 1.0,
            }
        )
    reason = None if exact else "unit_test_dependency_unavailable"
    profile = {"p50_ms": 1.0, "p95_ms": 1.0, "p99_ms": 1.0}
    cell = {
        "adapter": adapter,
        "corpus_size": size,
        "selectivity": fraction,
        "status": "ok" if exact else "skipped",
        "reason": reason,
        "metrics": metrics,
        "metric_provenance": {
            key: {
                "status": "measured" if value is not None else "unavailable",
                "source": "unit_test_exact_fixture" if value is not None else None,
                "reason": None if value is not None else (reason or "not_measured"),
            }
            for key, value in metrics.items()
        },
        "latency_profiles": {
            "cold": dict(profile) if exact else dict.fromkeys(profile),
            "warm": dict(profile) if exact else dict.fromkeys(profile),
        },
        "adoption": {
            "adopt": False,
            "becomes_default": False,
            "requires_measurement": True,
            "reasons": ["exact ground truth" if exact else reason],
        },
        "filter_methodology": {
            "indexed_corpus_size": size,
            "selected_corpus_size": max(1, round(size * fraction)),
            "application": "query_time",
            "equivalent_predicate": "selected = true",
            "implementation": (
                "lancedb-prefilter"
                if adapter.startswith("lancedb")
                else "numpy-mask"
                if exact
                else "postfilter"
            ),
        },
        "index": {
            "requested": (
                "exact"
                if exact
                else "ann"
                if adapter in {"usearch", "lancedb-ann"}
                else "flat"
            ),
            "status": "flat" if exact else "unavailable",
            "type": "exact-numpy" if exact else None,
            "verified_by": "unit_test_fixture" if exact else None,
            "reason": reason,
        },
    }
    if exact:
        cell["_exact_p95_ms"] = 1.0
    return cell


def test_required_scale_matrix_artifacts_exist() -> None:
    assert RUNNER.is_file()
    assert REPORT_SCHEMA.is_file()
    assert FULL_REPORT_SCHEMA.is_file()


def test_closed_corpus_sizes_and_selectivity_are_exported() -> None:
    runner = _runner()
    assert runner.CORPUS_SIZES == CORPUS_SIZES
    assert runner.SELECTIVITY_FRACTIONS == SELECTIVITY
    assert runner.ADAPTER_IDS == ADAPTER_IDS
    assert runner.CRASH_POINTS == CRASH_POINTS
    assert runner.ADOPTION_RECALL_FLOOR == 0.98
    assert runner.ADOPTION_LATENCY_SPEEDUP_FLOOR == 2.0
    assert runner.PRODUCT_P95_TARGET_MS > 0
    assert runner.MATERIAL_EXCEED_RATIO > 1


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


def test_exact_top_k_breaks_partition_boundary_ties_by_id() -> None:
    np = pytest.importorskip("numpy")
    runner = _runner()
    vectors = np.ones((60, 2), dtype=np.float32)
    ids = [f"id-{index:03d}" for index in reversed(range(60))]

    result = runner.exact_numpy_search(vectors, np.ones(2), k=50, ids=ids)

    assert result.ids == tuple(sorted(ids)[:50])


def test_recall_uses_true_exact_top_50_ground_truth() -> None:
    runner = _runner()
    relevant = [f"id-{index:03d}" for index in range(50)]
    retrieved = relevant[:10] + [f"miss-{index:03d}" for index in range(40)]
    assert runner.recall_at_k(retrieved, relevant, 10) == 1.0
    assert runner.recall_at_k(retrieved, relevant, 50) == 0.2

    lower_ranked_truth = relevant[10:20]
    assert runner.recall_at_k(lower_ranked_truth, relevant, 10) == 0.0


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
        provenance = cell["metric_provenance"][key]
        assert provenance["status"] in {"measured", "unavailable"}
        if provenance["status"] == "unavailable":
            assert cell["metrics"][key] is None
            assert provenance["reason"]
        else:
            assert provenance["source"]
    assert "cold" in cell["latency_profiles"]
    assert "warm" in cell["latency_profiles"]
    for side in ("p50_ms", "p95_ms", "p99_ms"):
        assert side in cell["latency_profiles"]["cold"]
        assert side in cell["latency_profiles"]["warm"]
    assert cell["metrics"]["disk_bytes"] is None
    assert cell["metric_provenance"]["disk_bytes"]["status"] == "unavailable"


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
        assert payload["worker_returncode"] != 0
        assert payload["platform"] == sys.platform
        assert payload["filesystem"]
        assert payload["status"] in {"passed", "failed", "unavailable"}
        if payload["injected"] is False:
            assert payload["status"] == "unavailable"
            assert payload["worker_returncode"] in {81, 82}
        if payload["status"] == "passed":
            assert payload["injected"] is True
            assert payload["recovered_cleanly"] is True
            assert payload["partial_activation"] is False


def test_crash_errors_are_failures_not_success(monkeypatch) -> None:
    runner = _runner()

    def broken_run(*_args, **_kwargs):
        raise OSError("injected subprocess error")

    monkeypatch.setattr(runner.subprocess, "run", broken_run)
    outcome = runner.run_crash_matrix(points=("before_fsync",))["before_fsync"]
    assert outcome["status"] == "failed"
    assert outcome["recovered_cleanly"] is False


def test_crash_worker_that_did_not_reach_injection_claims_no_fsync(monkeypatch) -> None:
    runner = _runner()
    result = types.SimpleNamespace(returncode=1, stderr="worker failed before injection")
    monkeypatch.setattr(runner.subprocess, "run", lambda *_args, **_kwargs: result)

    outcome = runner.run_crash_matrix(points=("before_activation",))["before_activation"]

    assert outcome["status"] == "failed"
    assert outcome["injected"] is False
    assert outcome["file_fsync"] == "unavailable"
    assert outcome["directory_fsync"] == "unavailable"


def test_before_fsync_crash_does_not_require_directory_fsync(monkeypatch) -> None:
    runner = _runner()
    result = types.SimpleNamespace(returncode=91, stderr="")
    monkeypatch.setattr(runner.subprocess, "run", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(
        runner, "_fsync_directory", lambda _path: (False, "directory_fsync_unsupported")
    )

    outcome = runner.run_crash_matrix(points=("before_fsync",))["before_fsync"]

    assert outcome["status"] == "passed"
    assert outcome["file_fsync"] == "not_reached"
    assert outcome["directory_fsync"] == "not_reached"
    assert outcome["recovery_directory_fsync"] == "not_reached"


def test_worker_reports_unavailable_when_required_directory_fsync_is_unavailable(
    monkeypatch,
) -> None:
    runner = _runner()
    result = types.SimpleNamespace(returncode=81, stderr="directory fsync unsupported")
    monkeypatch.setattr(runner.subprocess, "run", lambda *_args, **_kwargs: result)

    outcome = runner.run_crash_matrix(points=("before_activation",))["before_activation"]

    assert outcome["status"] == "unavailable"
    assert outcome["injected"] is False
    assert outcome["file_fsync"] == "performed"
    assert outcome["directory_fsync"] == "unavailable"


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
    assert any("latency" in reason for reason in decision["reasons"])

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
        exact_p95_ms=300.0,
        candidate_p95_ms=100.0,
    )
    assert decision["adopt"] is True
    assert decision["becomes_default"] is False
    assert decision["requires_measurement"] is True


@pytest.mark.parametrize("bad", [None, math.nan, math.inf, -math.inf, 0.0, -1.0, "1"])
def test_adoption_gate_rejects_invalid_or_nonfinite_metrics(bad) -> None:
    runner = _runner()
    values = {
        "recall_at_10": 0.99,
        "recall_at_50": 0.99,
        "exact_p95_ms": runner.PRODUCT_P95_TARGET_MS * runner.MATERIAL_EXCEED_RATIO * 2,
        "candidate_p95_ms": 1.0,
    }
    for field in values:
        invalid = dict(values)
        invalid[field] = bad
        assert runner.evaluate_adoption_gate(**invalid)["adopt"] is False


def test_adoption_requires_exact_to_materially_exceed_product_target() -> None:
    runner = _runner()
    decision = runner.evaluate_adoption_gate(
        recall_at_10=0.99,
        recall_at_50=0.99,
        exact_p95_ms=runner.PRODUCT_P95_TARGET_MS,
        candidate_p95_ms=runner.PRODUCT_P95_TARGET_MS / 3,
    )
    assert decision["adopt"] is False
    assert any("product p95" in reason for reason in decision["reasons"])


def test_adoption_requires_candidate_to_meet_product_target() -> None:
    runner = _runner()
    decision = runner.evaluate_adoption_gate(
        recall_at_10=0.99,
        recall_at_50=0.99,
        exact_p95_ms=1000.0,
        candidate_p95_ms=200.0,
    )
    assert decision["adopt"] is False
    assert any("candidate p95" in reason for reason in decision["reasons"])


def test_adoption_rejects_nonfinite_computed_speedup() -> None:
    runner = _runner()
    decision = runner.evaluate_adoption_gate(
        recall_at_10=0.99,
        recall_at_50=0.99,
        exact_p95_ms=sys.float_info.max,
        candidate_p95_ms=sys.float_info.min,
    )

    assert decision["adopt"] is False
    assert decision["measured_speedup"] is None
    assert any("speedup" in reason and "nonfinite" in reason for reason in decision["reasons"])


def test_smoke_report_validates_against_schema() -> None:
    from reliable_memory import SchemaValidationError, validate_schema

    runner = _runner()
    report = runner.run_smoke(corpus_size=24, dimensions=4, seed=9, queries=3)
    validate_schema(report, REPORT_SCHEMA)
    # Full matrix sizes are declared but smoke must not expand to heavy corpora.
    assert report["declared_corpus_sizes"] == list(CORPUS_SIZES)
    assert report["executed_corpus_sizes"] == [24]
    assert report["heavy_parallel"] is False
    report["crash_matrix"]["before_fsync"]["status"] = "invented_success"
    with pytest.raises(SchemaValidationError):
        validate_schema(report, REPORT_SCHEMA)

    report = runner.run_smoke(corpus_size=24, dimensions=4, seed=9, queries=3)
    report["cells"][0]["selectivity"] = 0.0
    with pytest.raises(SchemaValidationError):
        validate_schema(report, REPORT_SCHEMA)

    report = runner.run_smoke(
        corpus_size=24, dimensions=4, seed=9, queries=3, adapters=("exact-numpy",)
    )
    report["cells"][0]["metrics"]["recall_at_10"] = 1.01
    with pytest.raises(SchemaValidationError):
        validate_schema(report, REPORT_SCHEMA)


def test_full_report_has_separate_closed_schema(tmp_path) -> None:
    from reliable_memory import SchemaValidationError, validate_schema

    runner = _runner()
    smoke = runner.run_smoke(corpus_size=16, dimensions=4, queries=2, adapters=("exact-numpy",))
    incomplete = runner.build_report(
        mode="full",
        executed_corpus_sizes=CORPUS_SIZES,
        dimensions=8,
        queries=50,
        seed=0,
        cells=smoke["cells"],
        crash_matrix=runner.unavailable_crash_matrix("not_run_in_unit_test"),
    )
    with pytest.raises(SchemaValidationError):
        validate_schema(incomplete, FULL_REPORT_SCHEMA)

    cells = []
    for size in CORPUS_SIZES:
        for fraction in SELECTIVITY:
            for adapter in ADAPTER_IDS:
                cells.append(_fake_full_cell(adapter, size, fraction))
    report = runner.build_report(
        mode="full",
        executed_corpus_sizes=CORPUS_SIZES,
        dimensions=8,
        queries=50,
        seed=0,
        cells=cells,
        crash_matrix=runner.unavailable_crash_matrix("not_run_in_unit_test"),
    )
    assert all("_exact_p95_ms" not in cell for cell in report["cells"])
    assert all("adopted" not in cell and "is_default" not in cell for cell in report["cells"])
    validate_schema(report, FULL_REPORT_SCHEMA)
    output = tmp_path / "full.json"
    runner.write_report_atomic(report, output, mode="full")
    assert json.loads(output.read_text(encoding="utf-8"))["schema_version"] == (
        "scale-matrix-full/v1"
    )
    with pytest.raises(SchemaValidationError):
        validate_schema(report, REPORT_SCHEMA)
    assert report["schema_version"] == "scale-matrix-full/v1"
    assert report["mode"] == "full"
    report["executed_corpus_sizes"] = [1, 2, 3, 4, 5]
    with pytest.raises(SchemaValidationError):
        validate_schema(report, FULL_REPORT_SCHEMA)


def test_full_atomic_report_rejects_duplicate_matrix_cells(tmp_path) -> None:
    runner = _runner()
    cells = []
    for size in CORPUS_SIZES:
        for fraction in SELECTIVITY:
            for adapter in ADAPTER_IDS:
                cells.append(_fake_full_cell(adapter, size, fraction))
    cells[-1] = json.loads(json.dumps(cells[0]))
    report = runner.build_report(
        mode="full",
        executed_corpus_sizes=CORPUS_SIZES,
        dimensions=4,
        queries=1,
        seed=0,
        cells=cells,
        crash_matrix=runner.unavailable_crash_matrix("unit_test"),
    )

    with pytest.raises(ValueError, match="full matrix cells"):
        runner.write_report_atomic(report, tmp_path / "full.json", mode="full")
    assert not (tmp_path / "full.json").exists()


def test_smoke_atomic_report_rejects_duplicate_adapter_cells(tmp_path) -> None:
    runner = _runner()
    report = runner.run_smoke(
        corpus_size=16, dimensions=4, queries=2, adapters=("exact-numpy",)
    )
    report["cells"].append(json.loads(json.dumps(report["cells"][0])))

    with pytest.raises(ValueError, match="smoke matrix cells"):
        runner.write_report_atomic(report, tmp_path / "smoke.json", mode="smoke")
    assert not (tmp_path / "smoke.json").exists()


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
    env = {
        **dict(**{k: v for k, v in __import__("os").environ.items()}),
        "PYTHONPATH": str(ROOT / "scripts"),
    }
    r1 = subprocess.run(
        cmd + ["--output", str(out_a)], capture_output=True, text=True, env=env, check=False
    )
    r2 = subprocess.run(
        cmd + ["--output", str(out_b)], capture_output=True, text=True, env=env, check=False
    )
    assert r1.returncode == 0, r1.stderr
    assert r2.returncode == 0, r2.stderr
    a = json.loads(out_a.read_text(encoding="utf-8"))
    b = json.loads(out_b.read_text(encoding="utf-8"))
    # Drop wall-clock fields that may jitter slightly.
    for report in (a, b):
        report.pop("generated_at", None)
        for cell in report.get("cells", []):
            cell.get("metrics", {}).pop("rss_bytes", None)
            cell.get("metrics", {}).pop("disk_bytes", None)
            cell.get("metrics", {}).pop("startup_ms", None)
            cell.get("metrics", {}).pop("build_ms", None)
            cell.get("metrics", {}).pop("update_ms", None)
            cell.get("metrics", {}).pop("delete_ms", None)
            cell.get("metrics", {}).pop("concurrent_reader_p95_ms", None)
            cell.get("metrics", {}).pop("batch_throughput_qps", None)
            cell.get("metrics", {}).pop("latency_ms", None)
            # Adoption reasons include the measured p95 values normalized above.
            cell.pop("adoption", None)
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


def test_optional_adapter_indexes_full_corpus_and_filters_at_query(monkeypatch) -> None:
    runner = _runner()
    corpus = runner.generate_corpus(n_chunks=64, dimensions=4, seed=3)
    mask = runner.selectivity_mask(corpus, fraction=0.1, seed=3)
    observed = {}

    def fake_adapter(**kwargs):
        observed.update(kwargs)
        metrics = {key: None for key in METRIC_KEYS}
        metrics.update(
            {
                "latency_ms": 1.0,
                "build_ms": 1.0,
                "recall_at_10": 1.0,
                "recall_at_50": 1.0,
                "batch_throughput_qps": 1.0,
            }
        )
        return {
            "adapter": "usearch",
            "corpus_size": len(kwargs["corpus"].chunk_ids),
            "selectivity": kwargs["selectivity"],
            "status": "ok",
            "reason": None,
            "metrics": metrics,
            "latency_profiles": {
                "cold": {"p50_ms": 1.0, "p95_ms": 1.0, "p99_ms": 1.0},
                "warm": {"p50_ms": 1.0, "p95_ms": 1.0, "p99_ms": 1.0},
            },
            "adoption": {
                "adopt": False,
                "becomes_default": False,
                "requires_measurement": True,
                "reasons": [],
            },
        }

    monkeypatch.setattr(runner, "_adapter_available", lambda _id: True)
    monkeypatch.setattr(runner, "_run_usearch_cell", fake_adapter)
    cell = runner.run_adapter(
        "usearch",
        corpus=corpus,
        queries=corpus.vectors[:2],
        k=50,
        mask=mask,
        selectivity=0.1,
        exact_p95_ms=1000.0,
    )
    assert len(observed["corpus"].chunk_ids) == 64
    assert int(observed["mask"].sum()) < 64
    assert cell["filter_methodology"]["indexed_corpus_size"] == 64
    assert cell["filter_methodology"]["application"] == "query_time"
    assert cell["filter_methodology"]["implementation"] == "postfilter"


@pytest.mark.parametrize(
    ("reported_type", "indexed_rows", "expected_status"),
    [("IVF_PQ", 16, "ann"), ("IVF_PQ", 15, "unavailable"), (None, 16, "unavailable")],
)
def test_fake_lance_adapter_never_labels_unverified_index_ann(
    monkeypatch, reported_type, indexed_rows, expected_status
) -> None:
    runner = _runner()
    corpus = runner.generate_corpus(n_chunks=16, dimensions=4, seed=4)
    captured = {}

    class Query:
        def where(self, predicate, prefilter):
            captured["predicate"] = (predicate, prefilter)
            return self

        def limit(self, value):
            captured["limit"] = value
            return self

        def to_list(self):
            return [
                {"id": row_id}
                for row_id, selected in zip(captured["rows"]["id"], captured["rows"]["selected"])
                if selected
            ][: captured["limit"]]

    class Table:
        def create_index(self, *args, **kwargs):
            captured["create_index"] = (args, kwargs)

        def list_indices(self):
            return [types.SimpleNamespace(name="vector_idx", index_type=reported_type)]

        def index_stats(self, _name):
            return types.SimpleNamespace(
                index_type=reported_type,
                num_indexed_rows=indexed_rows,
                num_unindexed_rows=16 - indexed_rows,
            )

        def search(self, _query):
            return Query()

    class Database:
        def create_table(self, _name, rows):
            captured["rows"] = rows
            return Table()

    lance = types.ModuleType("lancedb")
    lance.connect = lambda _path: Database()
    lance_index = types.ModuleType("lancedb.index")

    class IvfPq:
        def __init__(self, **kwargs):
            self.options = kwargs

    lance_index.IvfPq = IvfPq
    arrow = types.ModuleType("pyarrow")
    arrow.table = lambda rows: rows
    monkeypatch.setitem(sys.modules, "lancedb", lance)
    monkeypatch.setitem(sys.modules, "lancedb.index", lance_index)
    monkeypatch.setitem(sys.modules, "pyarrow", arrow)
    monkeypatch.setattr(runner, "_adapter_available", lambda _id: True)

    cell = runner.run_adapter(
        "lancedb-ann",
        corpus=corpus,
        queries=corpus.vectors[:1],
        k=10,
        mask=runner.selectivity_mask(corpus, fraction=0.1, seed=4),
        selectivity=0.1,
        exact_p95_ms=1000.0,
    )

    assert len(captured["rows"]["id"]) == 16
    create_args, create_kwargs = captured["create_index"]
    assert create_args == ("vector",)
    assert isinstance(create_kwargs["config"], IvfPq)
    assert create_kwargs["config"].options["distance_type"] == "cosine"
    assert cell["index"]["status"] == expected_status
    if expected_status == "ann":
        assert captured["predicate"] == ("selected = true", True)
    else:
        assert cell["status"] == "skipped"


def test_fake_lance_adapter_verifies_the_vector_index_not_the_first_index(monkeypatch) -> None:
    runner = _runner()
    corpus = runner.generate_corpus(n_chunks=16, dimensions=4, seed=4)

    class Query:
        def where(self, _predicate, prefilter):
            assert prefilter is True
            return self

        def limit(self, _value):
            return self

        def to_list(self):
            return []

    class Table:
        def create_index(self, *_args, **_kwargs):
            return None

        def list_indices(self):
            return [
                types.SimpleNamespace(name="selected_idx", index_type="BTREE"),
                types.SimpleNamespace(name="vector_idx", index_type="IVF_PQ"),
            ]

        def index_stats(self, name):
            if name == "selected_idx":
                return types.SimpleNamespace(
                    index_type="BTREE", num_indexed_rows=16, num_unindexed_rows=0
                )
            return types.SimpleNamespace(
                index_type="IVF_PQ", num_indexed_rows=16, num_unindexed_rows=0
            )

        def search(self, _query):
            return Query()

    class Database:
        def create_table(self, _name, _rows):
            return Table()

    lance = types.ModuleType("lancedb")
    lance.connect = lambda _path: Database()
    lance_index = types.ModuleType("lancedb.index")
    lance_index.IvfPq = lambda **kwargs: kwargs
    arrow = types.ModuleType("pyarrow")
    arrow.table = lambda rows: rows
    monkeypatch.setitem(sys.modules, "lancedb", lance)
    monkeypatch.setitem(sys.modules, "lancedb.index", lance_index)
    monkeypatch.setitem(sys.modules, "pyarrow", arrow)
    monkeypatch.setattr(runner, "_adapter_available", lambda _id: True)

    cell = runner.run_adapter(
        "lancedb-ann",
        corpus=corpus,
        queries=corpus.vectors[:1],
        k=10,
        mask=runner.selectivity_mask(corpus, fraction=0.1, seed=4),
        selectivity=0.1,
        exact_p95_ms=1000.0,
    )

    assert cell["status"] == "ok"
    assert cell["index"]["status"] == "ann"
    assert cell["index"]["type"] == "IVF_PQ"


@pytest.mark.parametrize(
    ("connectivity", "metric", "expected_status"),
    [(16, "cos", "ann"), (0, "cos", "unavailable"), (16, "l2sq", "unavailable")],
)
def test_fake_usearch_adapter_requires_hnsw_configuration(
    monkeypatch, connectivity, metric, expected_status
) -> None:
    np = pytest.importorskip("numpy")
    runner = _runner()
    corpus = runner.generate_corpus(n_chunks=16, dimensions=4, seed=4)

    class Matches:
        keys = np.arange(15, -1, -1, dtype=np.int64)

    class Index:
        def __init__(self, **_kwargs):
            self.connectivity = connectivity
            self.metric = metric
            self.size = 0

        def add(self, keys, _vectors):
            self.size = len(keys)
            return None

        def search(self, _query, _count):
            return Matches()

    package = types.ModuleType("usearch")
    module = types.ModuleType("usearch.index")
    module.Index = Index
    package.index = module
    monkeypatch.setitem(sys.modules, "usearch", package)
    monkeypatch.setitem(sys.modules, "usearch.index", module)
    monkeypatch.setattr(runner, "_adapter_available", lambda _id: True)
    rss_values = iter((123, None))
    monkeypatch.setattr(runner, "_rss_bytes", lambda: next(rss_values))

    mask = np.zeros(16, dtype=bool)
    mask[[1, 5, 9, 13]] = True
    cell = runner.run_adapter(
        "usearch",
        corpus=corpus,
        queries=corpus.vectors[:1],
        k=4,
        mask=mask,
        selectivity=0.25,
        exact_p95_ms=1000.0,
    )

    assert cell["index"]["status"] == expected_status
    if expected_status == "ann":
        assert cell["status"] == "ok"
        assert cell["filter_methodology"]["indexed_corpus_size"] == 16
        assert cell["filter_methodology"]["selected_corpus_size"] == 4
        assert cell["metrics"]["recall_at_10"] == 1.0
        assert cell["metrics"]["recall_at_50"] == 1.0
        assert cell["metrics"]["rss_bytes"] == 123
        assert cell["metric_provenance"]["rss_bytes"]["status"] == "measured"
    else:
        assert cell["status"] == "failed"


def test_usearch_filter_application_is_included_in_cold_latency(monkeypatch) -> None:
    pytest.importorskip("numpy")
    runner = _runner()
    corpus = runner.generate_corpus(n_chunks=8, dimensions=4, seed=4)

    class SlowKeys:
        def __iter__(self):
            time.sleep(0.02)
            return iter(range(8))

    class Index:
        connectivity = 16
        metric = "cos"
        size = 0

        def __init__(self, **_kwargs):
            pass

        def add(self, keys, _vectors):
            self.size = len(keys)

        def search(self, _query, _count):
            return types.SimpleNamespace(keys=SlowKeys())

    package = types.ModuleType("usearch")
    module = types.ModuleType("usearch.index")
    module.Index = Index
    package.index = module
    monkeypatch.setitem(sys.modules, "usearch", package)
    monkeypatch.setitem(sys.modules, "usearch.index", module)
    monkeypatch.setattr(runner, "_adapter_available", lambda _id: True)

    cell = runner.run_adapter(
        "usearch",
        corpus=corpus,
        queries=corpus.vectors[:1],
        k=4,
        exact_p95_ms=1000.0,
    )

    assert cell["latency_profiles"]["cold"]["p50_ms"] >= 15.0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"corpus_size": 0}, "corpus_size"),
        ({"dimensions": 0}, "dimensions"),
        ({"queries": 0}, "queries"),
    ],
)
def test_smoke_rejects_nonpositive_resource_bounds(kwargs, message) -> None:
    runner = _runner()
    values = {"corpus_size": 16, "dimensions": 4, "queries": 2}
    values.update(kwargs)
    with pytest.raises(ValueError, match=message):
        runner.run_smoke(**values)


@pytest.mark.parametrize("bad", [True, 1.5, "16"])
def test_smoke_rejects_noninteger_resource_bounds(bad) -> None:
    runner = _runner()
    with pytest.raises(ValueError, match="corpus_size"):
        runner.run_smoke(corpus_size=bad, dimensions=4, queries=2)


def test_matrix_and_measurement_resource_bounds_are_strictly_positive() -> None:
    runner = _runner()
    corpus = runner.generate_corpus(n_chunks=8, dimensions=4, seed=0)
    with pytest.raises(ValueError, match="corpus_sizes"):
        runner.plan_matrix(
            corpus_sizes=(0,),
            adapters=("exact-numpy",),
            selectivity=(1.0,),
        )
    with pytest.raises(ValueError, match="queries"):
        runner.run_adapter(
            "exact-numpy",
            corpus=corpus,
            queries=corpus.vectors[:0],
            k=1,
        )
    with pytest.raises(ValueError, match="repeats"):
        runner._time_search(lambda: None, repeats=0)
    with pytest.raises(ValueError, match="fraction"):
        runner.selectivity_mask(corpus, fraction=True, seed=0)
    with pytest.raises(ValueError, match="selectivity"):
        runner.plan_matrix(
            corpus_sizes=(8,), adapters=("exact-numpy",), selectivity=(True,)
        )
    with pytest.raises(ValueError, match="selectivity"):
        runner.run_adapter(
            "exact-numpy",
            corpus=corpus,
            queries=corpus.vectors[:1],
            k=1,
            selectivity=math.nan,
        )
    with pytest.raises(ValueError, match="k"):
        runner.exact_numpy_search(corpus.vectors, corpus.vectors[0], k=True)
    with pytest.raises(ValueError, match="mask length"):
        runner.run_adapter(
            "exact-numpy",
            corpus=corpus,
            queries=corpus.vectors[:1],
            k=1,
            mask=[True],
        )


def test_cli_validates_before_atomic_durable_write(tmp_path) -> None:
    runner = _runner()
    output = tmp_path / "report.json"
    report = runner.run_smoke(corpus_size=16, dimensions=4, queries=2)
    runner.write_report_atomic(report, output, mode="smoke")
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert not list(tmp_path.glob("*.tmp"))

    report["mode"] = "full"
    with pytest.raises(Exception):
        runner.write_report_atomic(report, output, mode="smoke")
    assert json.loads(output.read_text(encoding="utf-8"))["mode"] == "smoke"


@pytest.mark.parametrize(
    "argv",
    [
        ["--smoke", "--full"],
        ["--full", "--queries", str(min(CORPUS_SIZES) + 1)],
    ],
)
def test_cli_rejects_ambiguous_or_unexecutable_full_requests(argv) -> None:
    runner = _runner()
    with pytest.raises(SystemExit):
        runner.main(argv)


@pytest.mark.parametrize(
    ("metric", "value", "status", "source", "reason"),
    [
        ("update_ms", 0.0, "unavailable", None, "not_measured"),
        ("latency_ms", None, "measured", "exact-numpy", None),
        ("latency_ms", 1.0, "measured", None, None),
        ("update_ms", None, "unavailable", None, None),
    ],
)
def test_atomic_output_rejects_metric_provenance_mismatch(
    tmp_path, metric, value, status, source, reason
) -> None:
    runner = _runner()
    report = runner.run_smoke(
        corpus_size=16,
        dimensions=4,
        queries=2,
        adapters=("exact-numpy",),
    )
    report["cells"][0]["metrics"][metric] = value
    report["cells"][0]["metric_provenance"][metric]["status"] = status
    report["cells"][0]["metric_provenance"][metric]["source"] = source
    report["cells"][0]["metric_provenance"][metric]["reason"] = reason

    with pytest.raises(ValueError, match="metric provenance"):
        runner.write_report_atomic(report, tmp_path / "report.json", mode="smoke")


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
