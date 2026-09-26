"""A scenario that measured nothing names why in the report (2026-09-26, CI run 36252530355).

docs/research/2026-09-26-an-ownership-probe-says-why-it-measured-nothing.md
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

BENCHMARK_ROOT = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK_ROOT) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_ROOT))

import run_code_navigation as benchmark_runner  # noqa: E402
from run_code_navigation import _FixtureRun, _RealNavigationRuntime  # noqa: E402


def _runtime(error: BaseException) -> _RealNavigationRuntime:
    runtime = object.__new__(_RealNavigationRuntime)
    runtime._cleanup_failed = False

    def run(scenario: str, deadline: float) -> int:
        raise error

    runtime._run_ownership_scenario = run
    runtime._reset = lambda deadline: 0
    return runtime


def _reason(error: BaseException) -> str:
    runtime = _runtime(error)
    runtime._ownership_outcome("timeout", time.monotonic() + 60)
    return runtime.ownership_reasons["timeout"]


def test_each_way_of_measuring_nothing_has_its_own_name() -> None:
    wrong = RuntimeError("ownership probe reached the wrong terminal")
    wrong.__cause__ = ValueError("not a timeout")

    reasons = [_reason(benchmark_runner._ProbeRacedError("answered first")), _reason(wrong)]

    assert reasons == ["raced", "RuntimeError:ValueError"]


def test_the_report_names_the_reason_of_an_unavailable_scenario() -> None:
    run = object.__new__(_FixtureRun)
    run.runtime = _runtime(benchmark_runner._ProbeRacedError("answered first"))
    run.runtime._ownership_outcome("timeout", time.monotonic() + 60)
    run.errors = []
    run.ownership = {
        "timeout": {"available": False, "orphan_count": None},
        "crash": {"available": True, "orphan_count": 0},
    }

    run._name_unmeasured_ownership()

    assert run.errors == [{"phase": "ownership:timeout", "code": "raced"}]
