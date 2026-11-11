"""Benchmark run evidence: performance samples, cold readiness, final close and operator aggregates."""

from __future__ import annotations

import hashlib
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

BENCHMARK_ROOT = Path(__file__).resolve().parent.parent / "benchmark"
SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
for path in (BENCHMARK_ROOT, SCRIPTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import navigation_benchmark_metrics as metrics  # noqa: E402
import navigation_benchmark_queries as queries  # noqa: E402
from code_navigation import (  # noqa: E402
    NavigationResult,
    NavigationStatus,
    ResolutionLabel,
)
from generate_python_qualification import (  # noqa: E402
    PYRIGHT_VERSION,
    GoldQuery,
    QualificationRepository,
)
from navigation_benchmark_gates import evaluate_gates  # noqa: E402
from navigation_benchmark_git import (  # noqa: E402
    QualifiedIdentityError,
)
from run_code_navigation import (  # noqa: E402
    BenchmarkDependencies,
    run_fixture_benchmark,
)

from tests.code_navigation_benchmark_helpers import PACKAGE_SHA256  # noqa: E402
from tests.code_navigation_benchmark_runtime_helpers import (  # noqa: E402
    BehavioralNavigationRuntime,
    _qualified_identity,
)


def _location_identity(locations) -> set:
    """What a direct answer points at, independently of how it is packaged."""
    return {
        (
            location.path,
            location.line,
            location.character,
            location.byte_start,
            location.byte_end,
        )
        for location in locations
    }


def _answers_wrong(failure_mode: str, direct_calls: int) -> bool:
    """Every call for one mode, every second call for the other."""
    if failure_mode == "direct_wrong":
        return True
    if failure_mode != "direct_second_wrong":
        return False
    return direct_calls % 2 == 0


@pytest.mark.parametrize(
    "failure_mode",
    [
        "direct",
        "direct_empty",
        "direct_wrong",
        "direct_second_wrong",
        "status",
        "exact",
        "citation",
    ],
)
def test_performance_samples_require_successful_direct_and_exact_facade_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    real_current_citation = queries._current_citation
    runtimes: list[BehavioralNavigationRuntime] = []
    citation_failed = False

    def current_citation(*args, **kwargs) -> bool:
        nonlocal citation_failed
        valid = real_current_citation(*args, **kwargs)
        performance_call = runtimes and 400 < len(runtimes[0].calls) <= 440
        if failure_mode == "citation" and performance_call and not citation_failed:
            citation_failed = True
            return False
        return valid

    monkeypatch.setattr(queries, "_current_citation", current_citation)

    class PerformanceRuntime(BehavioralNavigationRuntime):
        def __init__(self, repository: QualificationRepository, scope: object) -> None:
            super().__init__(repository, scope)
            self.direct_calls = 0

        def direct_query(self, request: object, *, deadline: float) -> object:
            assert deadline > time.monotonic()
            self.direct_calls += 1
            query = self._query_for_request(request)
            assert query is not None
            result = self._direct_result_for_query(query)
            if failure_mode == "direct":
                return SimpleNamespace(
                    coverage="not_ready",
                    partial=True,
                    locations=result.locations,
                )
            if failure_mode == "direct_empty":
                return SimpleNamespace(
                    coverage="provider_reported",
                    partial=False,
                    locations=(),
                )
            return self._wrong_or_real(result, query)

        def _wrong_or_real(self, result: object, query: GoldQuery) -> object:
            if _answers_wrong(failure_mode, self.direct_calls):
                return self._direct_result_for_query(self._other_query(query))
            return result

        def _other_query(self, query: GoldQuery) -> GoldQuery:
            """A gold query that points somewhere else than this one."""
            expected = _location_identity(query.expected_locations)
            return next(
                candidate
                for candidate in self.repository.gold_queries
                if _location_identity(candidate.expected_locations) != expected
            )

        def query(self, request: object, *, deadline: float) -> NavigationResult:
            result = super().query(request, deadline=deadline)
            if failure_mode == "status" and 400 < len(self.calls) <= 440:
                return replace(
                    result,
                    status=NavigationStatus.ERROR,
                    effective_capability=None,
                )
            if failure_mode == "exact" and 400 < len(self.calls) <= 440:
                return replace(
                    result,
                    total=0,
                    locations=(),
                    resolution=ResolutionLabel.UNRESOLVED,
                )
            return result

    def runtime_factory(repository, scope, identity, state_root):
        runtime = PerformanceRuntime(repository, scope)
        runtimes.append(runtime)
        return runtime

    report = run_fixture_benchmark(
        tmp_path / f"performance-{failure_mode}",
        state_root=tmp_path / "state",
        mode="qualification",
        dependencies=BenchmarkDependencies(
            discover_identity=lambda scope, state_root, deadline: _qualified_identity(),
            runtime_factory=runtime_factory,
        ),
    )

    expected_samples = 19 if failure_mode == "citation" else 0
    assert report["performance"]["available"] is False
    assert report["performance"]["sample_count"] == expected_samples
    if expected_samples == 0:
        assert report["performance"]["warm_facade_p50_ms"] is None
        assert report["performance"]["warm_facade_p95_ms"] is None
        assert report["performance"]["direct_pyright_p95_ms"] is None
        assert report["performance"]["warm_overhead_p95_ms"] is None


def test_warm_performance_pair_uses_two_counterbalanced_repetitions() -> None:
    events: list[tuple[str, float]] = []
    request = object()
    direct_results = [object(), object()]
    facade_results = [object(), object()]
    direct_index = 0
    facade_index = 0

    class Runtime:
        def direct_query(self, value: object, *, deadline: float) -> object:
            nonlocal direct_index
            assert value is request
            events.append(("direct", deadline))
            result = direct_results[direct_index]
            direct_index += 1
            return result

        def query(self, value: object, *, deadline: float) -> object:
            nonlocal facade_index
            assert value is request
            events.append(("facade", deadline))
            result = facade_results[facade_index]
            facade_index += 1
            return result

    ticks = iter((0.000, 0.010, 0.020, 0.040, 0.050, 0.074, 0.080, 0.094))
    deadlines = iter((101.0, 102.0, 103.0, 104.0))

    measured = metrics._measure_warm_performance_pair(
        Runtime(),
        request,
        next_deadline=lambda: next(deadlines),
        perf_counter=lambda: next(ticks),
    )

    assert events == [
        ("direct", 101.0),
        ("facade", 102.0),
        ("facade", 103.0),
        ("direct", 104.0),
    ]
    assert measured[0] == tuple(direct_results)
    assert measured[1] == tuple(facade_results)
    assert measured[2] == pytest.approx(12.0)
    assert measured[3] == pytest.approx(22.0)


def test_cold_readiness_requires_a_successful_exact_first_result(tmp_path: Path) -> None:
    class ColdFailureRuntime(BehavioralNavigationRuntime):
        def direct_query(self, request: object, *, deadline: float) -> object:
            return super().direct_query(request, deadline=deadline)

        def query(self, request: object, *, deadline: float) -> NavigationResult:
            result = super().query(request, deadline=deadline)
            if len(self.calls) == 1:
                return replace(
                    result,
                    status=NavigationStatus.ERROR,
                    effective_capability=None,
                )
            return result

    report = run_fixture_benchmark(
        tmp_path / "cold-failure",
        state_root=tmp_path / "state",
        mode="qualification",
        dependencies=BenchmarkDependencies(
            discover_identity=lambda scope, state_root, deadline: _qualified_identity(),
            runtime_factory=lambda repository, scope, identity, state_root: ColdFailureRuntime(
                repository,
                scope,
            ),
        ),
    )

    assert report["performance"]["cold_readiness_seconds"] is None
    assert report["performance"]["sample_count"] == 20
    assert report["performance"]["available"] is False


@pytest.mark.parametrize(
    "first_outcome",
    [RuntimeError("first close failed"), 1],
    ids=["raises", "leak-signal"],
)
def test_final_runtime_close_retries_once_but_keeps_scenario_unavailable(
    tmp_path: Path,
    first_outcome: object,
) -> None:
    runtimes: list[BehavioralNavigationRuntime] = []

    class RetryingCloseRuntime(BehavioralNavigationRuntime):
        def __init__(self, repository: QualificationRepository, scope: object) -> None:
            super().__init__(repository, scope)
            self.close_outcomes = [first_outcome, 0]
            self.close_deadlines: list[float] = []

        def close(self, *, deadline: float) -> int:
            self.close_deadlines.append(deadline)
            outcome = self.close_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    def runtime_factory(repository, scope, identity, state_root):
        runtime = RetryingCloseRuntime(repository, scope)
        runtimes.append(runtime)
        return runtime

    report = run_fixture_benchmark(
        tmp_path / f"retrying-final-close-{type(first_outcome).__name__}",
        state_root=tmp_path / "state",
        mode="correctness-only",
        dependencies=BenchmarkDependencies(
            discover_identity=lambda scope, state_root, deadline: _qualified_identity(),
            runtime_factory=runtime_factory,
        ),
    )

    assert len(runtimes[0].close_deadlines) == 2
    assert report["reliability"]["ownership"]["normal_shutdown"] == {
        "available": False,
        "orphan_count": None,
    }
    assert report["reliability"]["orphan_process_count"] is None
    assert report["reliability"]["orphan_process_rate"] is None
    assert any(error["phase"] == "final_close" for error in report["errors"])
    assert evaluate_gates(report)["passed"] is False


def test_final_runtime_close_retry_failure_retains_unknown_evidence(tmp_path: Path) -> None:
    runtimes: list[BehavioralNavigationRuntime] = []

    class FailingCloseRuntime(BehavioralNavigationRuntime):
        def __init__(self, repository: QualificationRepository, scope: object) -> None:
            super().__init__(repository, scope)
            self.close_calls = 0

        def close(self, *, deadline: float) -> int:
            self.close_calls += 1
            raise RuntimeError("closed final close failure")

    def runtime_factory(repository, scope, identity, state_root):
        runtime = FailingCloseRuntime(repository, scope)
        runtimes.append(runtime)
        return runtime

    report = run_fixture_benchmark(
        tmp_path / "failing-final-close",
        state_root=tmp_path / "state",
        mode="correctness-only",
        dependencies=BenchmarkDependencies(
            discover_identity=lambda scope, state_root, deadline: _qualified_identity(),
            runtime_factory=runtime_factory,
        ),
    )

    assert runtimes[0].close_calls == 2
    assert report["reliability"]["ownership"]["normal_shutdown"] == {
        "available": False,
        "orphan_count": None,
    }
    assert report["reliability"]["orphan_process_count"] is None
    assert report["evidence"]["ownership_checks"] == 3
    assert {error["phase"] for error in report["errors"]} >= {
        "final_close",
        "final_close_retry",
    }
    assert evaluate_gates(report)["passed"] is False


@pytest.mark.parametrize(
    "identity",
    [
        SimpleNamespace(
            status="missing",
            qualified=False,
            version=None,
            package_sha256=None,
            node_version=None,
            node_major=None,
            degradation_codes=("pyright_missing",),
        ),
        SimpleNamespace(
            status="qualified",
            qualified=True,
            version="1.1.410",
            package_sha256=PACKAGE_SHA256,
            node_version="v22.15.0",
            node_major=22,
            degradation_codes=(),
        ),
        SimpleNamespace(
            status="qualified",
            qualified=True,
            version=PYRIGHT_VERSION,
            package_sha256=None,
            node_version="v22.15.0",
            node_major=22,
            degradation_codes=(),
        ),
    ],
)
def test_runner_rejects_missing_or_wrong_qualified_identity(
    tmp_path: Path,
    identity: SimpleNamespace,
) -> None:
    with pytest.raises(QualifiedIdentityError):
        run_fixture_benchmark(
            tmp_path / identity.status / str(identity.version),
            state_root=tmp_path / "state",
            mode="correctness-only",
            dependencies=BenchmarkDependencies(
                discover_identity=lambda scope, state_root, deadline: identity,
                runtime_factory=lambda *args: pytest.fail("runtime must not be constructed"),
            ),
        )


def test_operator_corpus_is_aggregate_only_and_never_leaks_private_data(tmp_path: Path) -> None:
    private = (tmp_path / "private-corpus").resolve()
    private.mkdir()
    secret_text = "PRIVATE_SENTINEL_SOURCE_TEXT"
    (private / "secret_project_name.py").write_text(secret_text, encoding="utf-8")

    dependencies = BenchmarkDependencies(
        discover_identity=lambda scope, state_root, deadline: _qualified_identity(),
        runtime_factory=lambda repository, scope, identity, state_root: BehavioralNavigationRuntime(
            repository, scope
        ),
        operator_probe=lambda path, state_root, deadline: {
            "available": True,
            "python_files": 1,
            "queries_attempted": 1,
            "queries_succeeded": 1,
            "errors": 0,
        },
    )
    report = run_fixture_benchmark(
        tmp_path / "operator-work",
        state_root=tmp_path / "state",
        mode="correctness-only",
        dependencies=dependencies,
        operator_corpus=private,
    )
    encoded = json.dumps(report, sort_keys=True)
    assert str(private) not in encoded
    assert private.name not in encoded
    assert "secret_project_name" not in encoded
    assert secret_text not in encoded
    assert hashlib.sha256(str(private).encode()).hexdigest() not in encoded
    assert report["operator_corpus"] == {
        "available": True,
        "python_files": 1,
        "queries_attempted": 1,
        "queries_succeeded": 1,
        "errors": 0,
    }
