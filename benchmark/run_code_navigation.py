"""Measured qualification runner for the Python code-navigation facade."""

from __future__ import annotations

import argparse
import os
import platform
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_ROOT = ROOT / "scripts"
BENCHMARK_ROOT = Path(__file__).resolve().parent
for _path in (SCRIPTS_ROOT, BENCHMARK_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from code_navigation import (  # noqa: E402
    NavigationRequest,
    NavigationResult,
    NavigationStatus,
)
from code_navigation_renderer import estimate_tokens, render_navigation  # noqa: E402
from generate_python_qualification import (  # noqa: E402
    CALL_QUERIES,
    DEFINITION_QUERIES,
    FIXTURE_LINES,
    FIXTURE_SEED,
    MUTATION_CYCLES,
    REFERENCE_QUERIES,
    GoldQuery,
    QualificationRepository,
    generate_qualification_repository,
)
from navigation_benchmark_deadlines import (  # noqa: E402
    BenchmarkTimeoutError,
    _check_run_deadline,
    _fresh_cleanup_deadline,
    _operation_deadline,
)
from navigation_benchmark_environment import (  # noqa: E402
    _environment,
    _peak_rss,
)
from navigation_benchmark_gates import evaluate_gates  # noqa: E402
from navigation_benchmark_git import (  # noqa: E402
    FixtureIdentityError,
    QualifiedIdentityError,
    _require_qualified_identity,
    initialize_deterministic_git,
    verify_repository_identity,
)
from navigation_benchmark_metrics import (  # noqa: E402
    PERFORMANCE_SAMPLES,
    _measure_warm_performance_pair,
    _performance_queries,
    nearest_rank_percentile,
    precision_recall_f1,
)
from navigation_benchmark_mutation import (  # noqa: E402
    FRESHNESS_CHECKS_PER_CYCLE,
    _mutate_and_measure,
)
from navigation_benchmark_operator_corpus import (  # noqa: E402
    _operator_link_or_reparse,
    _operator_metrics,
    _probe_operator_corpus,
)
from navigation_benchmark_ownership import (  # noqa: E402
    _OWNERSHIP_SCENARIOS,
    _RealNavigationRuntime,
)
from navigation_benchmark_queries import (  # noqa: E402
    DEFAULT_LIMIT,
    _actual_key,
    _citation_flags,
    _citations_current,
    _direct_results_are_exact,
    _expected_hashes,
    _expected_key,
    _navigation_assertion_succeeds,
    _navigation_request,
    _rendered_item_count,
    _token_record,
)
from navigation_benchmark_schema import (  # noqa: E402
    _canonical_json,
    load_manifest,
    validate_manifest,
    validate_report,
)
from pyright_profile import discover_pyright  # noqa: E402
from repository_scope import (  # noqa: E402
    RepositoryScope,
    resolve_repository_scope,
)

REPORT_SCHEMA_VERSION = "code-navigation-python-report/v1"
RUNNER_EVIDENCE_VERSION = "code-navigation-real/v1"
CRASH_CYCLES = 20
MAX_ESTIMATED_TOKENS = 1200
RUN_TIMEOUT_SECONDS = 13.0 * 60.0


@dataclass(frozen=True, slots=True)
class BenchmarkDependencies:
    """Injectable orchestration boundaries used by fast behavioral tests."""

    discover_identity: Callable[[RepositoryScope, Path, float], object]
    runtime_factory: Callable[[QualificationRepository, RepositoryScope, object, Path], object]
    operator_probe: Callable[[Path, Path, float], dict[str, object]] | None = None
    monotonic: Callable[[], float] = time.monotonic


def _default_dependencies() -> BenchmarkDependencies:
    return BenchmarkDependencies(
        discover_identity=lambda scope, state_root, deadline: discover_pyright(
            scope,
            state_root=state_root,
            deadline=deadline,
        ),
        runtime_factory=lambda repository, scope, identity, state_root: _RealNavigationRuntime(
            repository,
            scope,
            identity,  # type: ignore[arg-type]
            state_root,
        ),
    )


def _runtime_close_incident(
    runtime: object,
    *,
    monotonic: Callable[[], float],
) -> int:
    incident = runtime.close(deadline=_fresh_cleanup_deadline(monotonic=monotonic))
    if isinstance(incident, bool) or not isinstance(incident, int) or incident not in {0, 1}:
        raise ValueError("runtime close must return a binary orphan incident")
    return incident


def _close_runtime_with_retry(
    runtime: object,
    errors: list[dict[str, str]],
    *,
    monotonic: Callable[[], float],
) -> int | None:
    try:
        incident = _runtime_close_incident(runtime, monotonic=monotonic)
        if incident == 0:
            return 0
        first_code = "OrphanOwnership"
    except Exception as exc:
        first_code = type(exc).__name__
    errors.append({"phase": "final_close", "code": first_code})
    try:
        retried = _runtime_close_incident(runtime, monotonic=monotonic)
        if retried:
            errors.append({"phase": "final_close_retry", "code": "OrphanOwnership"})
    except Exception as exc:
        errors.append({"phase": "final_close_retry", "code": type(exc).__name__})
    return None


def _result_successful(result: object) -> bool:
    return result is not None and result.status in {NavigationStatus.OK, NavigationStatus.PARTIAL}


def _actual_keys(result: object, successful: bool) -> set[tuple[object, ...]]:
    if not successful:
        return set()
    return {_actual_key(location) for location in result.locations}


def _recovered_correctly(recovered: object, repository_root: Path, crash_query: GoldQuery) -> bool:
    if not isinstance(recovered, NavigationResult):
        return False
    locations = {_actual_key(location) for location in recovered.locations}
    citations = _citations_current(repository_root, recovered.locations, _expected_hashes(crash_query))
    expected = {_expected_key(location) for location in crash_query.expected_locations}
    return _navigation_assertion_succeeds(recovered, expected=expected, actual=locations, citations_current=citations)


def _unavailable_ownership() -> dict[str, dict[str, int | bool | None]]:
    return {scenario: {"available": False, "orphan_count": None} for scenario in _OWNERSHIP_SCENARIOS}


def _normal_shutdown_measured(normal_shutdown: object) -> bool:
    if not isinstance(normal_shutdown, dict) or normal_shutdown.get("available") is not True:
        return False
    orphan = normal_shutdown.get("orphan_count")
    return isinstance(orphan, int) and not isinstance(orphan, bool) and orphan in {0, 1}


def _record_final_incident(ownership: dict, final_incident: int | None) -> None:
    normal_shutdown = ownership.get("normal_shutdown")
    if final_incident is None:
        ownership["normal_shutdown"] = {"available": False, "orphan_count": None}
        return
    if _normal_shutdown_measured(normal_shutdown):
        normal_shutdown["orphan_count"] = max(normal_shutdown["orphan_count"], final_incident)


def _orphan_process_count(orphan_values: list[object]) -> int | None:
    if len(orphan_values) != len(_OWNERSHIP_SCENARIOS):
        return None
    return sum(int(value) for value in orphan_values if isinstance(value, int))


def _ownership_summary(ownership: dict) -> tuple[int, int | None]:
    """(scenarios measured, orphan process count over all scenarios or None)."""
    measured = [value for value in ownership.values() if isinstance(value, dict) and value.get("available") is True]
    return len(measured), _orphan_process_count([value.get("orphan_count") for value in measured])


def _rate(numerator: float, denominator: float) -> float:
    if denominator:
        return numerator / denominator
    return 0.0


def _orphan_rate(orphan_process_count: int | None) -> float | None:
    if orphan_process_count is None:
        return None
    return orphan_process_count / len(_OWNERSHIP_SCENARIOS)


_NO_PERFORMANCE = {
    "available": False,
    "cold_readiness_seconds": None,
    "warm_facade_p50_ms": None,
    "warm_facade_p95_ms": None,
    "direct_pyright_p95_ms": None,
    "warm_overhead_p95_ms": None,
    "sample_count": 0,
}


class _FixtureRun:
    """One measured fixture run: its phases append to one error list in a fixed order."""

    def __init__(self, repository, scope, runtime, mode, run_deadline, monotonic) -> None:
        self.repository = repository
        self.scope = scope
        self.runtime = runtime
        self.mode = mode
        self.run_deadline = run_deadline
        self.monotonic = monotonic
        self.definition_attempted = 0
        self.definition_exact = 0
        self.reference_expected: set[tuple[object, ...]] = set()
        self.reference_actual: set[tuple[object, ...]] = set()
        self.call_expected: set[tuple[object, ...]] = set()
        self.call_actual: set[tuple[object, ...]] = set()
        self.query_attempts = 0
        self.tasks_solved = 0
        self.citation_total = 0
        self.citation_correct = 0
        self.token_tasks: list[dict[str, object]] = []
        self.default_items = 0
        self.default_tokens = 0
        self.errors: list[dict[str, str]] = []
        self.first_request: NavigationRequest | None = None
        self.cold_readiness_seconds: float | None = None
        self.ownership = _unavailable_ownership()
        self.warm_facade: list[float] = []
        self.direct_pyright: list[float] = []
        self.overhead: list[float] = []
        self.stale = 0
        self.mutation_cycles = 0
        self.freshness_checks_measured = 0
        self.freshness_latencies: list[float] = []
        self.crash_attempts = 0
        self.crash_recoveries = 0

    def check(self) -> None:
        _check_run_deadline(self.run_deadline, monotonic=self.monotonic)

    def operation_deadline(self) -> float:
        return _operation_deadline(self.run_deadline, monotonic=self.monotonic)

    # --- gold queries ------------------------------------------------------------

    def _query_result(self, request: NavigationRequest) -> NavigationResult | None:
        try:
            return self.runtime.query(request, deadline=self.operation_deadline())
        except BenchmarkTimeoutError:
            raise
        except Exception as exc:
            self.errors.append({"phase": "gold_query", "code": type(exc).__name__})
            return None

    def _pair_sets(self, capability: str) -> tuple[set, set]:
        if capability == "references":
            return self.reference_expected, self.reference_actual
        return self.call_expected, self.call_actual

    def _tally_correctness(self, query: GoldQuery, expected: set, actual: set) -> None:
        if query.capability == "definition":
            self.definition_attempted += 1
            if actual == expected:
                self.definition_exact += 1
            return
        target_expected, target_actual = self._pair_sets(query.capability)
        target_expected.update((query.query_id, *item) for item in expected)
        target_actual.update((query.query_id, *item) for item in actual)

    def _record_rendered(self, index: int, query, request, result, solved: bool, elapsed: float) -> None:
        if index == 0 and self.mode == "qualification" and solved:
            self.cold_readiness_seconds = elapsed
        rendered = render_navigation(result, offset=0, limit=DEFAULT_LIMIT)
        self.default_items = max(self.default_items, _rendered_item_count(rendered))
        self.default_tokens = max(self.default_tokens, estimate_tokens(_canonical_json(rendered)))
        if solved:
            self.tasks_solved += 1
            self.token_tasks.append(_token_record(query, request, result, rendered))

    def _gold_query(self, index: int, query: GoldQuery) -> None:
        request = _navigation_request(query, self.scope)
        if self.first_request is None:
            self.first_request = request
        started = time.perf_counter()
        self.query_attempts += 1
        result = self._query_result(request)
        self.check()
        elapsed = time.perf_counter() - started
        expected = {_expected_key(location) for location in query.expected_locations}
        successful = _result_successful(result)
        actual = _actual_keys(result, successful)
        self._tally_correctness(query, expected, actual)
        if result is None:
            return
        citations = _citation_flags(self.repository.root, query, result)
        self.citation_total += len(citations)
        self.citation_correct += sum(citations)
        solved = successful and _navigation_assertion_succeeds(
            result, expected=expected, actual=actual, citations_current=all(citations)
        )
        self._record_rendered(index, query, request, result, solved, elapsed)
        self.check()

    def gold_phase(self) -> None:
        for index, query in enumerate(self.repository.gold_queries):
            self._gold_query(index, query)

    # --- performance sample ------------------------------------------------------

    def _measure_pair(self, request: NavigationRequest):
        try:
            measured = _measure_warm_performance_pair(self.runtime, request, next_deadline=self.operation_deadline)
            self.check()
            return measured
        except BenchmarkTimeoutError:
            raise
        except Exception as exc:
            self.errors.append({"phase": "performance_sample", "code": type(exc).__name__})
            self.check()
            return None

    def _facade_valid(self, query: GoldQuery, facade_results) -> bool:
        expected = {_expected_key(location) for location in query.expected_locations}
        hashes = _expected_hashes(query)
        for facade_result in facade_results:
            actual = {_actual_key(location) for location in facade_result.locations}
            citations = _citations_current(self.repository.root, facade_result.locations, hashes)
            if not _navigation_assertion_succeeds(facade_result, expected=expected, actual=actual, citations_current=citations):
                return False
        return True

    def _pair_error(self, query: GoldQuery, direct_results, facade_results) -> dict[str, str] | None:
        if not _direct_results_are_exact(
            self.runtime, self.repository, self.scope, query, direct_results, deadline=self.operation_deadline()
        ):
            return {"phase": "performance_direct", "code": "UnsuccessfulDirectResult"}
        if not self._facade_valid(query, facade_results):
            return {"phase": "performance_facade", "code": "UnsuccessfulNavigationResult"}
        return None

    def _performance_query(self, query: GoldQuery) -> None:
        measured = self._measure_pair(_navigation_request(query, self.scope))
        if measured is None:
            return
        direct_results, facade_results, direct_ms, facade_ms = measured
        error = self._pair_error(query, direct_results, facade_results)
        if error is not None:
            self.errors.append(error)
            return
        self.direct_pyright.append(direct_ms)
        self.warm_facade.append(facade_ms)
        self.overhead.append(facade_ms - direct_ms)

    def performance_phase(self) -> None:
        if self.mode != "qualification":
            return
        for query in _performance_queries(self.repository.gold_queries):
            self._performance_query(query)

    # --- mutations, crashes, ownership ------------------------------------------

    def mutation_phase(self) -> None:
        (
            self.stale,
            self.mutation_cycles,
            self.freshness_checks_measured,
            self.freshness_latencies,
        ) = _mutate_and_measure(
            self.runtime, self.repository, self.scope, self.errors, run_deadline=self.run_deadline, monotonic=self.monotonic
        )

    def _crash_cycle(self, crash_request: NavigationRequest, crash_query: GoldQuery) -> bool:
        """One kill-and-recover cycle; False when cleanup became terminal."""
        self.crash_attempts += 1
        try:
            recovered = self.runtime.crash_and_recover(crash_request, deadline=self.operation_deadline())
            if _recovered_correctly(recovered, self.repository.root, crash_query):
                self.crash_recoveries += 1
        except BenchmarkTimeoutError:
            raise
        except Exception as exc:
            self.errors.append({"phase": "crash_recovery", "code": type(exc).__name__})
        self.check()
        if getattr(self.runtime, "cleanup_failed", False):
            self.errors.append({"phase": "cleanup", "code": "CleanupTerminal"})
            return False
        return True

    def crash_phase(self) -> None:
        if self.first_request is None:
            return
        crash_query = self.repository.gold_queries[-1]
        crash_request = _navigation_request(crash_query, self.scope)
        for _index in range(CRASH_CYCLES):
            if not self._crash_cycle(crash_request, crash_query):
                break

    def _name_unmeasured_ownership(self) -> None:
        """Each scenario that measured nothing says why, so a failed gate is diagnosable."""
        for scenario, outcome in self.ownership.items():
            if not outcome["available"]:
                self.errors.extend(self._unmeasured_entries(scenario))

    def _unmeasured_entries(self, scenario: str) -> list[dict[str, str]]:
        """The scenario's own reason and message, and a failed recovery's, when there was one."""
        reasons = getattr(self.runtime, "ownership_reasons", {})
        messages = getattr(self.runtime, "ownership_messages", {})
        entry = {"phase": f"ownership:{scenario}", "code": reasons.get(scenario, "not_attempted")}
        if scenario in messages:
            entry["message"] = messages[scenario]
        recovery = getattr(self.runtime, "ownership_recovery", {}).get(scenario)
        if recovery is None:
            return [entry]
        return [entry, {"phase": f"ownership:{scenario}:recovery", "code": recovery[0], "message": recovery[1]}]

    def ownership_phase(self) -> None:
        if getattr(self.runtime, "cleanup_failed", False):
            return
        try:
            self.ownership = self.runtime.ownership_checks(deadline=self.operation_deadline())
            self._name_unmeasured_ownership()
            self.check()
            if getattr(self.runtime, "cleanup_failed", False):
                self.errors.append({"phase": "ownership", "code": "CleanupTerminal"})
        except BenchmarkTimeoutError:
            raise
        except Exception as exc:
            self.errors.append({"phase": "ownership", "code": type(exc).__name__})
            self.ownership = _unavailable_ownership()
            self.check()

    def finish(self) -> None:
        final_incident = _close_runtime_with_retry(self.runtime, self.errors, monotonic=self.monotonic)
        _record_final_incident(self.ownership, final_incident)

    # --- report ------------------------------------------------------------------

    def performance(self) -> dict[str, object]:
        if self.mode != "qualification":
            return dict(_NO_PERFORMANCE)
        percentiles = (
            nearest_rank_percentile(self.warm_facade, 0.5),
            nearest_rank_percentile(self.warm_facade, 0.95),
            nearest_rank_percentile(self.direct_pyright, 0.95),
            nearest_rank_percentile(self.overhead, 0.95),
        )
        complete = all(value is not None for value in (self.cold_readiness_seconds, *percentiles))
        return {
            "available": len(self.warm_facade) == PERFORMANCE_SAMPLES and complete,
            "cold_readiness_seconds": self.cold_readiness_seconds,
            "warm_facade_p50_ms": percentiles[0],
            "warm_facade_p95_ms": percentiles[1],
            "direct_pyright_p95_ms": percentiles[2],
            "warm_overhead_p95_ms": percentiles[3],
            "sample_count": len(self.warm_facade),
        }

    def report(self, commit: str, identity: object, operator_metrics, resources: dict) -> dict[str, object]:
        ownership_checks, orphan_process_count = _ownership_summary(self.ownership)
        freshness_checks_attempted = MUTATION_CYCLES * FRESHNESS_CHECKS_PER_CYCLE
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "mode": self.mode,
            "identity": {
                "source_manifest_sha256": self.repository.source_manifest_sha256,
                "gold_sha256": self.repository.gold_sha256,
                "git_commit": commit,
                "python_version": platform.python_version(),
                "pyright_version": getattr(identity, "version"),
                "pyright_package_sha256": getattr(identity, "package_sha256"),
                "node_version": getattr(identity, "node_version"),
                "node_major": getattr(identity, "node_major"),
            },
            "environment": _environment(),
            "workload": {
                "fixture_seed": FIXTURE_SEED,
                "fixture_lines": FIXTURE_LINES,
                "definition_queries": DEFINITION_QUERIES,
                "reference_queries": REFERENCE_QUERIES,
                "call_queries": CALL_QUERIES,
                "edit_rename_delete_cycles": MUTATION_CYCLES,
                "crash_cycles": CRASH_CYCLES,
                "default_limit": DEFAULT_LIMIT,
                "max_estimated_tokens": MAX_ESTIMATED_TOKENS,
                "freshness_checks": freshness_checks_attempted,
                "ownership_checks": len(_OWNERSHIP_SCENARIOS),
                "ownership_scenarios": list(_OWNERSHIP_SCENARIOS),
            },
            "evidence": {
                "measured": True,
                "runner": RUNNER_EVIDENCE_VERSION,
                "source_hash_verified": True,
                "gold_hash_verified": True,
                "git_commit_verified": self.scope.git_commit == commit,
                "identity_verified": True,
                "query_attempts": self.query_attempts,
                "mutation_cycles": self.mutation_cycles,
                "crash_attempts": self.crash_attempts,
                "ownership_checks": ownership_checks,
            },
            "correctness": {
                "definitions": {
                    "attempted": self.definition_attempted,
                    "exact": self.definition_exact,
                    "accuracy": _rate(self.definition_exact, self.definition_attempted),
                },
                "references": {"attempted": REFERENCE_QUERIES, **precision_recall_f1(self.reference_expected, self.reference_actual)},
                "calls": {"attempted": CALL_QUERIES, **precision_recall_f1(self.call_expected, self.call_actual)},
                "task_success_rate": self.tasks_solved / len(self.repository.gold_queries),
                "citation_locations_attempted": self.citation_total,
                "citation_locations_correct": self.citation_correct,
                "citation_correctness_rate": _rate(self.citation_correct, self.citation_total),
            },
            "tokens": {
                "cache_read_label": "not_applicable_no_result_cache",
                "tasks": self.token_tasks,
                "default_items": self.default_items,
                "max_default_estimated_tokens": self.default_tokens,
            },
            "reliability": {
                "stale_answer_count": self.stale,
                "freshness_checks_attempted": freshness_checks_attempted,
                "freshness_checks_measured": self.freshness_checks_measured,
                "stale_result_rate": self.stale / freshness_checks_attempted,
                "mutation_cycles_measured": self.mutation_cycles,
                "edit_to_fresh_p50_ms": nearest_rank_percentile(self.freshness_latencies, 0.5),
                "edit_to_fresh_p95_ms": nearest_rank_percentile(self.freshness_latencies, 0.95),
                "crash_recoveries": self.crash_recoveries,
                "crash_attempts": self.crash_attempts,
                "recovery_rate": _rate(self.crash_recoveries, self.crash_attempts),
                "orphan_process_count": orphan_process_count,
                "orphan_checks_attempted": len(_OWNERSHIP_SCENARIOS),
                "orphan_checks_measured": ownership_checks,
                "orphan_process_rate": _orphan_rate(orphan_process_count),
                "ownership": self.ownership,
            },
            "performance": self.performance(),
            "resources": resources,
            "operator_corpus": operator_metrics,
            "errors": self.errors,
            "market_superiority_claimed": False,
        }


def _prepare_fixture(work_root: Path, state_root: Path, manifest, dependencies, run_deadline: float) -> tuple:
    """(manifest, repository, commit, scope, identity, runtime) of one generated fixture."""
    monotonic = dependencies.monotonic
    _check_run_deadline(run_deadline, monotonic=monotonic)
    manifest_value = dict(load_manifest() if manifest is None else manifest)
    validate_manifest(manifest_value)
    _check_run_deadline(run_deadline, monotonic=monotonic)
    work_root = Path(work_root)
    work_root.mkdir(parents=True, exist_ok=False)
    state_root = Path(state_root).resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    repository = generate_qualification_repository(work_root / "qualification")
    _check_run_deadline(run_deadline, monotonic=monotonic)
    verify_repository_identity(repository, manifest_value)
    _check_run_deadline(run_deadline, monotonic=monotonic)
    commit = initialize_deterministic_git(repository.root, deadline=run_deadline, monotonic=monotonic)
    _check_run_deadline(run_deadline, monotonic=monotonic)
    scope = resolve_repository_scope(repository.root)
    _check_run_deadline(run_deadline, monotonic=monotonic)
    if scope.git_commit != commit:
        raise FixtureIdentityError("resolved Git commit does not match generated commit")
    identity = dependencies.discover_identity(scope, state_root, _operation_deadline(run_deadline, monotonic=monotonic))
    _check_run_deadline(run_deadline, monotonic=monotonic)
    _require_qualified_identity(identity, manifest_value)
    runtime = dependencies.runtime_factory(repository, scope, identity, state_root)
    _check_run_deadline(run_deadline, monotonic=monotonic)
    return manifest_value, repository, commit, scope, identity, runtime


def _operator_corpus_metrics(operator_corpus: Path | None, dependencies, state_root: Path, run: _FixtureRun):
    if operator_corpus is None:
        return None
    probe = dependencies.operator_probe or _probe_operator_corpus
    metrics = _operator_metrics(probe(operator_corpus, Path(state_root).resolve(), run.operation_deadline()))
    run.check()
    return metrics


def _resources(mode: str, run: _FixtureRun) -> dict[str, object]:
    if mode != "qualification":
        return {"available": False, "client_peak_rss_mib": None, "method": "not_measured_in_correctness_only"}
    run.check()
    peak_rss, rss_method = _peak_rss()
    run.check()
    return {"available": peak_rss is not None, "client_peak_rss_mib": peak_rss, "method": rss_method}


def run_fixture_benchmark(
    work_root: Path,
    *,
    state_root: Path,
    mode: str,
    dependencies: BenchmarkDependencies | None = None,
    operator_corpus: Path | None = None,
    manifest: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Generate, identify, execute, and report one measured fixture run."""
    if mode not in {"correctness-only", "qualification"}:
        raise ValueError("mode must be correctness-only or qualification")
    dependencies = dependencies or _default_dependencies()
    run_deadline = dependencies.monotonic() + RUN_TIMEOUT_SECONDS
    _manifest, repository, commit, scope, identity, runtime = _prepare_fixture(
        work_root, state_root, manifest, dependencies, run_deadline
    )
    run = _FixtureRun(repository, scope, runtime, mode, run_deadline, dependencies.monotonic)
    try:
        run.gold_phase()
        run.performance_phase()
        run.mutation_phase()
        run.crash_phase()
        run.ownership_phase()
    finally:
        run.finish()
    operator_metrics = _operator_corpus_metrics(operator_corpus, dependencies, state_root, run)
    resources = _resources(mode, run)
    run.check()
    report = run.report(commit, identity, operator_metrics, resources)
    run.check()
    validate_report(report)
    run.check()
    return report


def resolve_state_root(
    argument: Path | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    environment = os.environ if environ is None else environ
    if argument is not None:
        return Path(argument).expanduser().resolve()
    if environment.get("LLM_WIKI_STATE_ROOT"):
        return Path(environment["LLM_WIKI_STATE_ROOT"]).expanduser().resolve()
    return ROOT.expanduser().resolve()


def _validated_operator_corpus(parser: argparse.ArgumentParser, argument: Path) -> Path:
    operator_corpus = argument.expanduser()
    if not operator_corpus.is_absolute():
        parser.error("--operator-corpus must be absolute")
    operator_corpus = Path(os.path.abspath(os.fspath(operator_corpus)))
    try:
        metadata = operator_corpus.stat(follow_symlinks=False)
    except OSError:
        parser.error("--operator-corpus must exist")
    if _operator_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        parser.error("--operator-corpus must be a directory")
    return operator_corpus


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run measured Python navigation qualification")
    parser.add_argument("--fixture", action="store_true", help="Use the pinned public fixture")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--correctness-only", action="store_true")
    modes.add_argument("--qualification", action="store_true")
    parser.add_argument("--require-gates", action="store_true")
    parser.add_argument("--operator-corpus", type=Path)
    parser.add_argument("--state-root", type=Path)
    args = parser.parse_args(argv)
    if not args.fixture:
        parser.error("--fixture is required")
    _validate_path_arguments(parser, args)
    return args


def _validate_path_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.operator_corpus is not None:
        args.operator_corpus = _validated_operator_corpus(parser, args.operator_corpus)
    if args.state_root is not None and not args.state_root.is_absolute():
        parser.error("--state-root must be absolute")


def _error_exit(code_name: str, exit_code: int, *, failed_checks: tuple[str, ...] = ()) -> int:
    report = {"status": "error", "code": code_name, "market_superiority_claimed": False}
    if failed_checks:
        report["failed_checks"] = list(failed_checks)
    print(_canonical_json(report), file=sys.stderr)
    return exit_code


def _run_and_evaluate(args: argparse.Namespace, mode: str, state_root: Path) -> int:
    with tempfile.TemporaryDirectory(prefix="code-navigation-fixture-") as temporary:
        report = run_fixture_benchmark(
            Path(temporary) / "run",
            state_root=state_root,
            mode=mode,
            operator_corpus=args.operator_corpus,
        )
    evaluation = evaluate_gates(report)
    print(_canonical_json(report))
    if args.require_gates and not evaluation["passed"]:
        print(_canonical_json(evaluation), file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    mode = "qualification" if args.qualification else "correctness-only"
    state_root = resolve_state_root(args.state_root)
    try:
        return _run_and_evaluate(args, mode, state_root)
    except BenchmarkTimeoutError:
        return _error_exit("BenchmarkTimeout", 4)
    except (FixtureIdentityError, QualifiedIdentityError) as exc:
        return _error_exit(type(exc).__name__, 2, failed_checks=getattr(exc, "failed_checks", ()))
    except Exception as exc:
        return _error_exit(type(exc).__name__, 3)


if __name__ == "__main__":
    raise SystemExit(main())
