"""Benchmark run orchestration: deadlines, cleanup, resources and the command line."""

from __future__ import annotations

import json
import os
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

import navigation_benchmark_deadlines as deadlines  # noqa: E402
import navigation_benchmark_git as git  # noqa: E402
import navigation_benchmark_schema as schema  # noqa: E402
import run_code_navigation as benchmark_runner  # noqa: E402
from code_intelligence import Capability  # noqa: E402
from code_navigation import (  # noqa: E402
    NavigationResult,
    NavigationStatus,
)
from generate_python_qualification import (  # noqa: E402
    FIXTURE_LINES,
    FIXTURE_SEED,
    PYRIGHT_VERSION,
    QualificationRepository,
)
from navigation_benchmark_gates import GATE_THRESHOLDS, evaluate_gates  # noqa: E402
from navigation_benchmark_git import (  # noqa: E402
    QualifiedIdentityError,
)
from navigation_benchmark_schema import (  # noqa: E402
    validate_report,
)
from run_code_navigation import (  # noqa: E402
    BenchmarkDependencies,
    parse_args,
    resolve_state_root,
    run_fixture_benchmark,
)

from tests.code_navigation_benchmark_helpers import _measured_report  # noqa: E402
from tests.code_navigation_benchmark_runtime_helpers import (  # noqa: E402
    BehavioralNavigationRuntime,
    _empty_navigation_result,
    _qualified_identity,
)


def test_report_records_machine_readable_platform_python_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(benchmark_runner.platform, "python_version", lambda: "3.14.42")

    report = run_fixture_benchmark(
        tmp_path / "python-version-identity",
        state_root=tmp_path / "state",
        mode="correctness-only",
        dependencies=BenchmarkDependencies(
            discover_identity=lambda scope, state_root, deadline: _qualified_identity(),
            runtime_factory=lambda repository, scope, identity, state_root: (
                BehavioralNavigationRuntime(repository, scope)
            ),
        ),
    )

    assert report["identity"]["python_version"] == "3.14.42"


def test_runner_queries_every_gold_item_and_metrics_follow_actual_results(tmp_path: Path) -> None:
    runtimes: list[BehavioralNavigationRuntime] = []

    def runtime_factory(repository, scope, identity, state_root):
        assert identity.qualified
        assert state_root == tmp_path / "state"
        runtime = BehavioralNavigationRuntime(repository, scope, mismatch_query_id="definition-000")
        runtimes.append(runtime)
        return runtime

    report = run_fixture_benchmark(
        tmp_path / "work",
        state_root=tmp_path / "state",
        mode="correctness-only",
        dependencies=BenchmarkDependencies(
            discover_identity=lambda scope, state_root, deadline: _qualified_identity(),
            runtime_factory=runtime_factory,
        ),
    )

    assert report["correctness"]["definitions"]["accuracy"] == 199 / 200
    assert report["correctness"]["references"]["f1"] == 1.0
    assert report["correctness"]["calls"]["f1"] == 1.0
    assert report["evidence"]["query_attempts"] == 400
    assert report["reliability"]["recovery_rate"] == 1.0
    baseline_calls = runtimes[0].calls[:400]
    assert len(baseline_calls) == 400
    assert sum(call.capability is Capability.DEFINITIONS for call in baseline_calls) == 200
    assert sum(call.capability is Capability.REFERENCES for call in baseline_calls) == 100
    assert sum(call.capability is Capability.CALLS for call in baseline_calls) == 100
    assert report["tokens"]["tasks"]
    assert all(task["cache_read_tokens"] == 0 for task in report["tokens"]["tasks"])
    assert report["tokens"]["cache_read_label"] == "not_applicable_no_result_cache"
    validate_report(report)


def test_complete_semantic_fake_run_passes_correctness_gates(tmp_path: Path) -> None:
    class AcceptedPartialRuntime(BehavioralNavigationRuntime):
        def query(self, request: object, *, deadline: float) -> NavigationResult:
            result = super().query(request, deadline=deadline)
            return replace(result, status=NavigationStatus.PARTIAL) if result.locations else result

    report = run_fixture_benchmark(
        tmp_path / "complete-fake",
        state_root=tmp_path / "state",
        mode="correctness-only",
        dependencies=BenchmarkDependencies(
            discover_identity=lambda scope, state_root, deadline: _qualified_identity(),
            runtime_factory=lambda repository, scope, identity, state_root: (
                AcceptedPartialRuntime(repository, scope)
            ),
        ),
    )

    validate_report(report)
    assert report["errors"] == []
    assert evaluate_gates(report)["passed"] is True


def test_run_deadline_never_renews_and_cleanup_gets_a_fresh_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClock:
        def __init__(self) -> None:
            self.value = 100.0

        def __call__(self) -> float:
            return self.value

        def advance(self, seconds: float) -> None:
            self.value += seconds

    clock = FakeClock()
    discovery_deadlines: list[float] = []
    query_deadlines: list[float] = []
    cleanup_deadlines: list[float] = []

    class DeadlineRuntime:
        cleanup_failed = False

        def query(self, request: object, *, deadline: float) -> NavigationResult:
            query_deadlines.append(deadline)
            clock.advance(30.0)
            return _empty_navigation_result(request)

        def close(self, *, deadline: float) -> int:
            cleanup_deadlines.append(deadline)
            return 0

    def discover(scope: object, state_root: Path, deadline: float) -> SimpleNamespace:
        discovery_deadlines.append(deadline)
        return _qualified_identity()

    # The fake clock also sizes the real timeout given to the fixture's git
    # commands, so a five-second run budget gave git five real seconds and a
    # loaded Windows runner could not finish inside it. The budget is spent by
    # advancing the clock per query instead.
    monkeypatch.setattr(benchmark_runner, "RUN_TIMEOUT_SECONDS", 50.0)
    monkeypatch.setattr(deadlines, "CLEANUP_TIMEOUT_SECONDS", 2.0)
    with pytest.raises(deadlines.BenchmarkTimeoutError):
        run_fixture_benchmark(
            tmp_path / "deadline",
            state_root=tmp_path / "state",
            mode="correctness-only",
            dependencies=BenchmarkDependencies(
                discover_identity=discover,
                runtime_factory=lambda *args: DeadlineRuntime(),
                monotonic=clock,
            ),
        )

    assert discovery_deadlines == [150.0]
    assert 1 <= len(query_deadlines) <= 2
    assert max(query_deadlines) <= 150.0
    assert cleanup_deadlines
    assert min(cleanup_deadlines) > 150.0


def test_every_benchmark_operation_receives_a_run_capped_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = time.monotonic()
    deadlines: list[tuple[str, float]] = []

    class RecordingRuntime(BehavioralNavigationRuntime):
        def query(self, request: object, *, deadline: float) -> NavigationResult:
            deadlines.append(("query", deadline))
            if request.path == "workloads/probe.py":
                deadlines.append(("mutation", deadline))
            return super().query(request, deadline=deadline)

        def direct_query(self, request: object, *, deadline: float) -> object:
            deadlines.append(("performance", deadline))
            return super().direct_query(request, deadline=deadline)

        def crash_and_recover(self, request: object, *, deadline: float) -> NavigationResult:
            deadlines.append(("crash", deadline))
            return self.query(request, deadline=deadline)

        def ownership_checks(self, *, deadline: float):
            deadlines.append(("ownership", deadline))
            return super().ownership_checks(deadline=deadline)

        def close(self, *, deadline: float) -> int:
            deadlines.append(("cleanup", deadline))
            return 0

    def discover(scope: object, state_root: Path, deadline: float) -> SimpleNamespace:
        deadlines.append(("discovery", deadline))
        return _qualified_identity()

    def operator(path: Path, state_root: Path, deadline: float) -> dict[str, object]:
        deadlines.append(("operator", deadline))
        return {
            "available": True,
            "python_files": 0,
            "queries_attempted": 0,
            "queries_succeeded": 0,
            "errors": 0,
        }

    monkeypatch.setattr(benchmark_runner, "RUN_TIMEOUT_SECONDS", 300.0)
    operator_root = tmp_path / "deadline-operator"
    operator_root.mkdir()
    run_fixture_benchmark(
        tmp_path / "all-deadlines",
        state_root=tmp_path / "state",
        mode="qualification",
        operator_corpus=operator_root,
        dependencies=BenchmarkDependencies(
            discover_identity=discover,
            runtime_factory=lambda repository, scope, identity, state_root: RecordingRuntime(
                repository, scope
            ),
            operator_probe=operator,
            monotonic=lambda: started,
        ),
    )

    measured = [(phase, deadline) for phase, deadline in deadlines if phase != "cleanup"]
    assert {phase for phase, _deadline in measured} >= {
        "discovery",
        "query",
        "performance",
        "mutation",
        "crash",
        "ownership",
        "operator",
    }
    assert all(deadline <= started + 300.0 for _phase, deadline in measured)


def test_cleanup_terminal_stops_crash_cycles_and_skips_ownership(
    tmp_path: Path,
) -> None:
    runtimes: list[BehavioralNavigationRuntime] = []

    class TerminalRuntime(BehavioralNavigationRuntime):
        cleanup_failed = False

        def __init__(self, repository: QualificationRepository, scope: object) -> None:
            super().__init__(repository, scope)
            self.crash_calls = 0
            self.ownership_calls = 0

        def crash_and_recover(self, request: object, *, deadline: float) -> None:
            self.crash_calls += 1
            self.cleanup_failed = True
            return None

        def ownership_checks(self, *, deadline: float):
            self.ownership_calls += 1
            return super().ownership_checks(deadline=deadline)

    def runtime_factory(repository, scope, identity, state_root):
        runtime = TerminalRuntime(repository, scope)
        runtimes.append(runtime)
        return runtime

    report = run_fixture_benchmark(
        tmp_path / "terminal-crash",
        state_root=tmp_path / "state",
        mode="correctness-only",
        dependencies=BenchmarkDependencies(
            discover_identity=lambda scope, state_root, deadline: _qualified_identity(),
            runtime_factory=runtime_factory,
        ),
    )

    assert runtimes[0].crash_calls == 1
    assert runtimes[0].ownership_calls == 0
    assert report["evidence"]["crash_attempts"] == 1
    assert report["evidence"]["ownership_checks"] == 0
    assert evaluate_gates(report)["passed"] is False


def test_cli_reports_sanitized_benchmark_timeout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def timed_out(*args, **kwargs):
        raise deadlines.BenchmarkTimeoutError("PRIVATE_TIMEOUT_DETAIL")

    monkeypatch.setattr(benchmark_runner, "run_fixture_benchmark", timed_out)
    result = benchmark_runner.main(["--fixture", "--correctness-only"])
    captured = capsys.readouterr()

    assert result != 0
    assert "BenchmarkTimeout" in captured.err
    assert "PRIVATE_TIMEOUT_DETAIL" not in captured.err


_NOT_READY_RESULT = ("not_ready", True)


_EMPTY_RESULT = ("provider_reported", False)


def test_peak_client_rss_is_measured_after_optional_operator_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class QualificationRuntime(BehavioralNavigationRuntime):
        def direct_query(self, request: object, *, deadline: float) -> object:
            return super().direct_query(request, deadline=deadline)

    def operator_probe(path: Path, state_root: Path, deadline: float) -> dict[str, object]:
        events.append("operator")
        return {
            "available": True,
            "python_files": 1,
            "queries_attempted": 1,
            "queries_succeeded": 1,
            "errors": 0,
        }

    def peak_rss() -> tuple[float, str]:
        events.append("rss")
        return 42.0, "measured-test"

    monkeypatch.setattr(benchmark_runner, "_peak_rss", peak_rss)
    operator_root = tmp_path / "operator-order"
    operator_root.mkdir()
    report = run_fixture_benchmark(
        tmp_path / "operator-order-work",
        state_root=tmp_path / "state",
        mode="qualification",
        operator_corpus=operator_root,
        dependencies=BenchmarkDependencies(
            discover_identity=lambda scope, state_root, deadline: _qualified_identity(),
            runtime_factory=lambda repository, scope, identity, state_root: QualificationRuntime(
                repository, scope
            ),
            operator_probe=operator_probe,
        ),
    )

    assert events == ["operator", "rss"]
    assert report["resources"]["client_peak_rss_mib"] == 42.0


def test_cli_sanitizes_unexpected_private_corpus_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private = (tmp_path / "private-corpus").resolve()
    private.mkdir()

    def fail_run(*args, **kwargs):
        raise OSError(f"private path failed: {private}")

    monkeypatch.setattr(benchmark_runner, "run_fixture_benchmark", fail_run)
    result = benchmark_runner.main(
        [
            "--fixture",
            "--correctness-only",
            "--operator-corpus",
            str(private),
        ]
    )
    captured = capsys.readouterr()
    assert result != 0
    assert str(private) not in captured.out
    assert str(private) not in captured.err


def test_cli_modes_fixture_and_operator_path_are_enforced(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        parse_args([])
    with pytest.raises(SystemExit):
        parse_args(["--fixture", "--correctness-only", "--qualification"])
    with pytest.raises(SystemExit):
        parse_args(["--correctness-only"])
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--fixture",
                "--correctness-only",
                "--operator-corpus",
                "relative/path",
            ]
        )
    missing = (tmp_path / "missing").resolve()
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--fixture",
                "--qualification",
                "--operator-corpus",
                str(missing),
            ]
        )
    assert parse_args(["--fixture", "--correctness-only"]).correctness_only


def test_require_gates_returns_nonzero_for_an_unmet_measured_gate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _measured_report("correctness-only")
    report["reliability"]["stale_answer_count"] = 1  # type: ignore[index]
    monkeypatch.setattr(
        benchmark_runner,
        "run_fixture_benchmark",
        lambda *args, **kwargs: report,
    )

    assert benchmark_runner.main(["--fixture", "--correctness-only"]) == 0
    assert benchmark_runner.main(["--fixture", "--correctness-only", "--require-gates"]) == 1
    capsys.readouterr()


def test_default_state_root_honors_environment_not_fixture_temp(tmp_path: Path) -> None:
    installed_state = (tmp_path / "installed-state").resolve()
    assert (
        resolve_state_root(None, environ={"LLM_WIKI_STATE_ROOT": str(installed_state)})
        == installed_state
    )
    explicit = (tmp_path / "explicit-state").resolve()
    assert (
        resolve_state_root(explicit, environ={"LLM_WIKI_STATE_ROOT": str(installed_state)})
        == explicit
    )
    fallback = resolve_state_root(None, environ={})
    assert fallback == BENCHMARK_ROOT.parent.resolve()
    assert "code-nav-" not in str(fallback)


def test_pinned_constants_match_approved_contract() -> None:
    assert FIXTURE_SEED == 411
    assert FIXTURE_LINES == 100_000
    assert PYRIGHT_VERSION == "1.1.411"
    assert GATE_THRESHOLDS == {
        "definition_accuracy": 0.99,
        "reference_f1": 0.95,
        "stale_answer_count": 0,
        "stale_result_rate": 0.0,
        "orphan_process_count": 0,
        "orphan_process_rate": 0.0,
        "recovery_rate": 1.0,
        "default_items": 10,
        "default_estimated_tokens": 1200,
        "warm_overhead_p95_ms": 30,
        "cold_readiness_seconds": 60,
        "client_rss_mib": 100,
    }
    assert os.path.isabs(str(BENCHMARK_ROOT))


@pytest.mark.parametrize(('field', 'value', 'failure'), [
    ('status', 'missing', 'status'), ('qualified', False, 'qualified'),
    ('version', 'wrong', 'pyright_version'),
    ('package_sha256', 'wrong', 'package_sha256'),
    ('node_major', 99, 'node_major'), ('node_version', '', 'node_version'),
    ('degradation_codes', ('private-value',), 'degradation_codes'),
])
def test_qualification_failure_names_the_failed_check(field, value, failure):
    manifest = schema.load_manifest()
    identity = SimpleNamespace(status='qualified', qualified=True,
        version=manifest['pyright_version'], package_sha256=manifest['pyright_package_sha256'],
        node_major=manifest['node_major'], node_version='v22.23.1', degradation_codes=())
    setattr(identity, field, value)
    with pytest.raises(QualifiedIdentityError) as caught:
        git._require_qualified_identity(identity, manifest)
    assert caught.value.failed_checks == (failure,)


def test_cli_keeps_qualification_checks_without_identity_values(monkeypatch, capsys):
    def fail(*args):
        identity = SimpleNamespace(status='private-status', qualified=False)
        git._require_qualified_identity(identity, schema.load_manifest())

    monkeypatch.setattr(benchmark_runner, '_run_and_evaluate', fail)
    assert benchmark_runner.main(['--fixture', '--correctness-only']) == 2
    captured = capsys.readouterr()
    report = json.loads(captured.err)
    assert report['failed_checks'] == ['status', 'qualified', 'pyright_version',
                                      'package_sha256', 'node_major', 'node_version']
    assert report['code'] == 'QualifiedIdentityError'
    assert report['market_superiority_claimed'] is False
    assert 'private-status' not in captured.err
