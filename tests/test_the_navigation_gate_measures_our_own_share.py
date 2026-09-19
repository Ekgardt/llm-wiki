"""The warm-overhead gate judges our layer's share of the work, not the runner's speed.

Every row below is a measured qualification run, read out of its CI job log or out of
`cache/benchmarks/full-2026-09-14/navigation_qualification.log`. Two of them were recorded
three hours apart on `ubuntu-latest`: between them Pyright itself — the control, which no
change of ours can reach — rose 27.6%, and the absolute bound flipped from pass to fail.
See `docs/research/2026-09-18-the-gate-measures-our-share-not-the-machine.md`.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

BENCHMARK_ROOT = Path(__file__).resolve().parent.parent / "benchmark"
SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
for _path in (BENCHMARK_ROOT, SCRIPTS_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import run_code_navigation as benchmark_runner  # noqa: E402
from run_code_navigation import GATE_THRESHOLDS, evaluate_gates, validate_report  # noqa: E402

from tests.test_code_navigation_benchmark import _measured_report  # noqa: E402

# run, cold readiness s, direct p95 ms, facade p50 ms, facade p95 ms, our overhead p95 ms
RECORDED_RUNS = (
    ("32235746281 2026-08-19", 0.842027, 44.104005, 61.916362, 64.753119, 22.075018),
    ("32239406567 2026-08-19", 0.850666, 46.033417, 63.854289, 67.796603, 22.163523),
    ("previous run 2026-09-18 15:33", 0.821574, 36.405852, 57.811063, 62.258476, 26.244401),
    ("35382495391 2026-09-18 18:50", 0.898590, 46.451831, 75.791264, 80.432178, 34.282478),
    ("local full run 2026-09-14", 0.759659, 26.262576, 47.376812, 51.162877, 24.900301),
)


def _recorded_report(
    cold_seconds: float,
    direct_p95: float,
    facade_p50: float,
    facade_p95: float,
    overhead_p95: float,
) -> dict:
    report = _measured_report()
    report["performance"] = {
        "available": True,
        "cold_readiness_seconds": cold_seconds,
        "warm_facade_p50_ms": facade_p50,
        "warm_facade_p95_ms": facade_p95,
        "direct_pyright_p95_ms": direct_p95,
        "warm_overhead_p95_ms": overhead_p95,
        "sample_count": 20,
    }
    validate_report(report)
    return report


def _warm_gate(report: dict) -> dict:
    return evaluate_gates(report)["gates"]["warm_overhead_p95_ms"]


@pytest.mark.parametrize(("run", "cold", "direct", "p50", "p95", "overhead"), RECORDED_RUNS)
def test_every_recorded_run_passes_including_the_one_the_runner_failed(
    run: str,
    cold: float,
    direct: float,
    p50: float,
    p95: float,
    overhead: float,
) -> None:
    """35382495391 failed at 34.28 ms against 30 while its control rose 27.6%."""
    gate = _warm_gate(_recorded_report(cold, direct, p50, p95, overhead))
    assert (run, gate["measured"], gate["passed"]) == (run, True, True)


def test_our_layer_taking_a_bigger_share_of_the_same_machine_still_fails() -> None:
    """The failing run's overhead, measured against the previous run's own slower control."""
    grown_overhead = RECORDED_RUNS[3][5]
    _, cold, direct, p50, p95, baseline_overhead = RECORDED_RUNS[2]
    report = _recorded_report(cold, direct, p50, p95, grown_overhead)
    gate = _warm_gate(report)
    grew = (grown_overhead > baseline_overhead, float(gate["threshold"]) < grown_overhead)
    assert (gate["passed"], grew) == (False, (True, True))
    assert evaluate_gates(report)["passed"] is False


def test_a_fast_machine_is_still_held_to_the_floor() -> None:
    """Where the scaled bound falls under 30 ms, 30 ms is what the gate uses."""
    _, cold, direct, p50, p95, _ = RECORDED_RUNS[4]
    report = _recorded_report(cold, direct, p50, p95, 30.000001)
    gate = _warm_gate(report)
    assert (gate["threshold"], gate["passed"]) == (30, False)


def test_the_published_bound_is_the_approved_floor_and_share() -> None:
    """Both halves of the bound are published in docs/CODE-NAVIGATION.md."""
    share = getattr(benchmark_runner, "WARM_OVERHEAD_MAX_SHARE_OF_DIRECT", None)
    assert (GATE_THRESHOLDS["warm_overhead_p95_ms"], share) == (30, 0.90)


def test_a_control_that_measured_nothing_fails_closed() -> None:
    """No control means no scaled bound and no qualification."""
    report = _recorded_report(*RECORDED_RUNS[2][1:])
    without_control = copy.deepcopy(report)
    without_control["performance"]["direct_pyright_p95_ms"] = 0.0
    validate_report(without_control)
    evaluation = evaluate_gates(without_control)
    assert (evaluation["evidence_complete"], evaluation["passed"]) == (False, False)
