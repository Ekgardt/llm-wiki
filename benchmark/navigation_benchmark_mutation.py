"""Mutation cycles that prove navigation answers stay fresh after the workload changes."""

from __future__ import annotations

import hashlib
import sys
import time
from collections.abc import Callable
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
BENCHMARK_ROOT = Path(__file__).resolve().parent
for _path in (SCRIPTS_ROOT, BENCHMARK_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from generate_python_qualification import (  # noqa: E402
    GoldLocation,
    GoldQuery,
    QualificationRepository,
)
from navigation_benchmark_deadlines import (  # noqa: E402
    QUERY_TIMEOUT_SECONDS,
    BenchmarkTimeoutError,
    _check_run_deadline,
    _operation_deadline,
)
from navigation_benchmark_queries import (  # noqa: E402
    _actual_key,
    _citations_current,
    _expected_hashes,
    _expected_key,
    _navigation_assertion_succeeds,
    _navigation_request,
    _query_position,
)
from repository_scope import RepositoryScope  # noqa: E402

FRESHNESS_CHECKS_PER_CYCLE = 5


def _mutation_expected(
    needle: bytes, target_path: str, target_content: bytes | None
) -> tuple[GoldLocation, ...]:
    """The declaration a mutation query must reach; none when the target is gone."""
    if target_content is None:
        return ()
    declaration = target_content.find(needle)
    if declaration < 0:
        raise ValueError("mutation workload target symbol is missing")
    target_line, target_character, _target_codepoint = _query_position(
        target_content, declaration
    )
    return (
        GoldLocation(
            target_path,
            target_line,
            target_character,
            declaration,
            declaration + len(needle),
            hashlib.sha256(target_content).hexdigest(),
        ),
    )


def _mutation_query(
    query_id: str,
    query_path: str,
    query_content: bytes,
    symbol: str,
    *,
    target_path: str,
    target_content: bytes | None,
) -> GoldQuery:
    needle = symbol.encode("utf-8")
    first_use = query_content.find(needle)
    use = query_content.find(needle, first_use + len(needle))
    if first_use < 0 or use < 0:
        raise ValueError("mutation workload probe symbol is missing")
    line, character, codepoint = _query_position(query_content, use)
    query_digest = hashlib.sha256(query_content).hexdigest()
    expected = _mutation_expected(needle, target_path, target_content)
    return GoldQuery(
        query_id,
        "definition",
        query_path,
        line,
        character,
        codepoint,
        use,
        use + len(needle),
        query_digest,
        symbol,
        None,
        expected,
    )


def _check_optional_deadline(run_deadline: float | None, monotonic: Callable[[], float]) -> None:
    if run_deadline is not None:
        _check_run_deadline(run_deadline, monotonic=monotonic)


def _reset_workload(
    repository: QualificationRepository, workload, run_deadline: float | None, monotonic: Callable[[], float]
) -> None:
    original = repository.root / workload.original_path
    renamed = repository.root / workload.renamed_path
    created = repository.root / workload.created_path
    probe = repository.root / workload.probe_path
    _check_optional_deadline(run_deadline, monotonic)
    renamed.unlink(missing_ok=True)
    created.unlink(missing_ok=True)
    original.write_bytes(workload.original_content)
    probe.write_bytes(workload.baseline_probe_content)
    _check_optional_deadline(run_deadline, monotonic)


def _mutation_steps(repository: QualificationRepository, workload) -> tuple:
    original = repository.root / workload.original_path
    renamed = repository.root / workload.renamed_path
    created = repository.root / workload.created_path
    probe = repository.root / workload.probe_path
    return (
        (
            "create",
            lambda: (
                created.write_bytes(workload.created_content),
                probe.write_bytes(workload.create_probe_content),
            ),
            _mutation_query(
                f"{workload.workload_id}-create",
                workload.probe_path,
                workload.create_probe_content,
                workload.created_symbol,
                target_path=workload.created_path,
                target_content=workload.created_content,
            ),
        ),
        (
            "edit",
            lambda: (
                original.write_bytes(workload.edited_content),
                probe.write_bytes(workload.edit_probe_content),
            ),
            _mutation_query(
                f"{workload.workload_id}-edit",
                workload.probe_path,
                workload.edit_probe_content,
                workload.edited_symbol,
                target_path=workload.original_path,
                target_content=workload.edited_content,
            ),
        ),
        (
            "rename_new",
            lambda: (
                original.rename(renamed),
                probe.write_bytes(workload.rename_probe_content),
            ),
            _mutation_query(
                f"{workload.workload_id}-rename-new",
                workload.probe_path,
                workload.rename_probe_content,
                workload.edited_symbol,
                target_path=workload.renamed_path,
                target_content=workload.edited_content,
            ),
        ),
        (
            "rename_old",
            lambda: probe.write_bytes(workload.rename_old_probe_content),
            _mutation_query(
                f"{workload.workload_id}-rename-old",
                workload.probe_path,
                workload.rename_old_probe_content,
                workload.edited_symbol,
                target_path=workload.original_path,
                target_content=None,
            ),
        ),
        (
            "delete",
            lambda: (
                renamed.unlink(),
                created.unlink(),
                probe.write_bytes(workload.delete_probe_content),
            ),
            _mutation_query(
                f"{workload.workload_id}-delete",
                workload.probe_path,
                workload.delete_probe_content,
                workload.edited_symbol,
                target_path=workload.renamed_path,
                target_content=None,
            ),
        ),
    )


def _step_deadline(run_deadline: float | None, monotonic: Callable[[], float]) -> float:
    if run_deadline is not None:
        return _operation_deadline(run_deadline, monotonic=monotonic)
    return monotonic() + QUERY_TIMEOUT_SECONDS


class _MutationTally:
    """What the mutation phase counts: stale answers, clean cycles, checks, latencies."""

    def __init__(self) -> None:
        self.stale = 0
        self.cycles_measured = 0
        self.checks_measured = 0
        self.latencies: list[float] = []


def _fresh_after_step(
    runtime: object,
    repository: QualificationRepository,
    scope: RepositoryScope,
    tally: _MutationTally,
    query: GoldQuery,
    deadline: float,
) -> bool:
    result = runtime.query(_navigation_request(query, scope), deadline=deadline)
    tally.checks_measured += 1
    actual = {_actual_key(location) for location in result.locations}
    expected = {_expected_key(location) for location in query.expected_locations}
    citations = _citations_current(repository.root, result.locations, _expected_hashes(query))
    return _navigation_assertion_succeeds(result, expected=expected, actual=actual, citations_current=citations)


def _run_mutation_step(
    runtime: object,
    repository: QualificationRepository,
    scope: RepositoryScope,
    tally: _MutationTally,
    errors: list[dict[str, str]],
    step: tuple,
    run_deadline: float | None,
    monotonic: Callable[[], float],
) -> bool:
    """One mutate-then-query step; False when it raised (the cycle is not clean)."""
    operation, mutate, query = step
    started = time.perf_counter()
    try:
        deadline = _step_deadline(run_deadline, monotonic)
        mutate()
        _check_optional_deadline(run_deadline, monotonic)
        if _fresh_after_step(runtime, repository, scope, tally, query, deadline):
            tally.latencies.append((time.perf_counter() - started) * 1000.0)
        else:
            tally.stale += 1
        _check_optional_deadline(run_deadline, monotonic)
        return True
    except BenchmarkTimeoutError:
        raise
    except Exception as exc:  # benchmark evidence retains a closed code only
        tally.stale += 1
        errors.append({"phase": f"mutation_{operation}", "code": type(exc).__name__})
        _check_optional_deadline(run_deadline, monotonic)
        return False


def _mutate_workload(
    runtime: object,
    repository: QualificationRepository,
    scope: RepositoryScope,
    tally: _MutationTally,
    errors: list[dict[str, str]],
    workload,
    run_deadline: float | None,
    monotonic: Callable[[], float],
) -> None:
    try:
        _reset_workload(repository, workload, run_deadline, monotonic)
    except BenchmarkTimeoutError:
        raise
    except Exception as exc:  # benchmark evidence retains a closed code only
        tally.stale += FRESHNESS_CHECKS_PER_CYCLE
        errors.append({"phase": "mutation_reset", "code": type(exc).__name__})
        return
    outcomes = [
        _run_mutation_step(runtime, repository, scope, tally, errors, step, run_deadline, monotonic)
        for step in _mutation_steps(repository, workload)
    ]
    if all(outcomes):
        tally.cycles_measured += 1


def _mutate_and_measure(
    runtime: object,
    repository: QualificationRepository,
    scope: RepositoryScope,
    errors: list[dict[str, str]],
    *,
    run_deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[int, int, int, list[float]]:
    tally = _MutationTally()
    for workload in repository.workloads:
        _mutate_workload(runtime, repository, scope, tally, errors, workload, run_deadline, monotonic)
    return tally.stale, tally.cycles_measured, tally.checks_measured, tally.latencies
