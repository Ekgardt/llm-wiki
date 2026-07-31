"""Deterministic qualification generator, manifest, and gate tests."""

from __future__ import annotations

import sys
from pathlib import Path

BENCHMARK_ROOT = Path(__file__).resolve().parent.parent / "benchmark"
SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
for path in (BENCHMARK_ROOT, SCRIPTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from generate_python_qualification import (  # noqa: E402
    FIXTURE_LINES,
    FIXTURE_SEED,
    PYRIGHT_VERSION,
    generate_qualification_repository,
)
from run_code_navigation import (  # noqa: E402
    GATE_SPECS,
    GATE_THRESHOLDS,
    degrade,
    evaluate_gates,
    load_manifest,
    passing_report,
)


def test_qualification_generator_is_exactly_100_000_lines(tmp_path: Path) -> None:
    first = generate_qualification_repository(tmp_path / "first")
    second = generate_qualification_repository(tmp_path / "second")
    assert first.line_count == second.line_count == 100_000
    assert first.source_manifest_sha256 == second.source_manifest_sha256
    assert FIXTURE_LINES == 100_000


def test_qualification_generator_seed_and_version_are_pinned() -> None:
    assert FIXTURE_SEED == 411
    assert PYRIGHT_VERSION == "1.1.411"


def test_manifest_matches_pinned_identity() -> None:
    manifest = load_manifest()
    assert manifest["schema_version"] == "code-navigation-python/v1"
    assert manifest["fixture_seed"] == 411
    assert manifest["fixture_lines"] == 100_000
    assert manifest["pyright_version"] == "1.1.411"
    assert manifest["node_major"] == 22
    assert manifest["market_superiority_claimed"] is False


def test_acceptance_gate_requires_every_production_threshold() -> None:
    report = passing_report()
    assert evaluate_gates(report)["passed"] is True
    for spec in GATE_SPECS:
        degraded = degrade(report, spec.field)
        assert evaluate_gates(degraded)["passed"] is False, spec.field


def test_gate_thresholds_are_exact() -> None:
    assert GATE_THRESHOLDS["definition_accuracy"] == 0.99
    assert GATE_THRESHOLDS["reference_f1"] == 0.95
    assert GATE_THRESHOLDS["stale_answer_count"] == 0
    assert GATE_THRESHOLDS["orphan_process_count"] == 0
    assert GATE_THRESHOLDS["recovery_rate"] == 1.0
    assert GATE_THRESHOLDS["default_items"] == 10
    assert GATE_THRESHOLDS["default_estimated_tokens"] == 1200
    assert GATE_THRESHOLDS["warm_overhead_p95_ms"] == 20
    assert GATE_THRESHOLDS["cold_readiness_seconds"] == 60
    assert GATE_THRESHOLDS["client_rss_mib"] == 100


def test_gold_queries_are_deterministic_and_reproducible(tmp_path: Path) -> None:
    first = generate_qualification_repository(tmp_path / "first")
    second = generate_qualification_repository(tmp_path / "second")
    assert first.gold_queries == second.gold_queries
    assert len(first.gold_queries) == 300
    assert sum(1 for q in first.gold_queries if q.capability == "definition") == 200
    assert sum(1 for q in first.gold_queries if q.capability == "references") == 100


def test_ambiguous_symbol_is_present(tmp_path: Path) -> None:
    repository = generate_qualification_repository(tmp_path / "repo")
    assert "execute" in repository.ambiguous_symbols


def test_fixture_emits_no_semantic_cache_tokens(tmp_path: Path) -> None:
    from run_code_navigation import _fixture_correctness_report

    report = _fixture_correctness_report(tmp_path / "fixture")
    assert report["cache_read_tokens"] == 0
    assert report["cache_read_label"] == "not_applicable_no_result_cache"
    assert report["market_superiority_claimed"] is False
