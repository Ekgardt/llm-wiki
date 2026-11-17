"""Mutation freshness checks and raw tool-token records of the Python navigation benchmark."""

from __future__ import annotations

import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

BENCHMARK_ROOT = Path(__file__).resolve().parent.parent / "benchmark"
SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
for path in (BENCHMARK_ROOT, SCRIPTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import navigation_benchmark_mutation as mutation  # noqa: E402
import navigation_benchmark_schema as schema  # noqa: E402
from code_intelligence import PositionRange  # noqa: E402
from code_navigation import (  # noqa: E402
    NavigationLocation,
    NavigationResult,
    NavigationStatus,
    Provenance,
    ResolutionLabel,
)
from code_navigation_renderer import estimate_tokens, render_navigation  # noqa: E402
from generate_python_qualification import (  # noqa: E402
    PYRIGHT_VERSION,
    GoldQuery,
    generate_qualification_repository,
)
from navigation_benchmark_git import (  # noqa: E402
    initialize_deterministic_git,
)
from navigation_benchmark_mutation import _mutate_and_measure  # noqa: E402
from navigation_benchmark_queries import _navigation_request, _token_record  # noqa: E402
from repository_scope import resolve_repository_scope  # noqa: E402

from tests.code_navigation_benchmark_runtime_helpers import (  # noqa: E402
    BehavioralNavigationRuntime,
    _empty_navigation_result,
    _result_with_locations,
)
from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402


def _partial_or_ok(partial: bool) -> NavigationStatus:
    if partial:
        return NavigationStatus.PARTIAL
    return NavigationStatus.OK


@pytest.mark.parametrize("partial_absence_call", [4, 5], ids=["rename-old", "delete"])
def test_partial_empty_mutation_absence_is_stale_but_positive_partial_is_valid(
    tmp_path: Path,
    partial_absence_call: int,
) -> None:
    repository = generate_qualification_repository(tmp_path / f"partial-{partial_absence_call}")
    repository = replace(repository, workloads=repository.workloads[:1])
    initialize_deterministic_git(repository.root)
    scope = resolve_repository_scope(repository.root)

    class PartialRuntime:
        def __init__(self) -> None:
            self.calls = 0

        def synchronize(self, *, deadline: float) -> None:
            assert deadline > time.monotonic()

        def query(self, request: object, *, deadline: float) -> NavigationResult:
            assert deadline > time.monotonic()
            self.calls += 1
            probe = repository.root / request.path
            import_line = probe.read_text(encoding="utf-8").splitlines()[0].split()
            module = import_line[1]
            symbol = import_line[3]
            target = repository.root / f"{module.replace('.', '/')}.py"
            locations: tuple[NavigationLocation, ...] = ()
            if target.is_file():
                content = target.read_bytes()
                byte_start = content.index(f"def {symbol}(".encode()) + len(b"def ")
                prefix = content[:byte_start]
                locations = (
                    NavigationLocation(
                        target.relative_to(repository.root).as_posix(),
                        PositionRange(byte_start, byte_start + len(symbol.encode())),
                        prefix.count(b"\n") + 1,
                        byte_start - (prefix.rfind(b"\n") + 1),
                        None,
                        None,
                        ResolutionLabel.LSP_CONFIRMED,
                        (Provenance("lsp", "pyright", PYRIGHT_VERSION, "provider_reported"),),
                    ),
                )
            partial = bool(locations) or self.calls == partial_absence_call
            status = _partial_or_ok(partial)
            return _result_with_locations(request, status, locations)

    errors: list[dict[str, str]] = []
    stale, cycles, measured, latencies = _mutate_and_measure(
        PartialRuntime(),
        repository,
        scope,
        errors,
    )

    assert stale == 1
    assert cycles == 1
    assert measured == 5
    assert len(latencies) == 4
    assert errors == []


def _query_id_list(queries) -> list:
    return [query.query_id for query in queries]


def _expected_location_counts_list(queries) -> list:
    return [len(query.expected_locations) for query in queries]


def _first_location_paths(queries) -> list:
    return [
        query.expected_locations[0].path
        for query in queries
        if query.expected_locations
    ]


def test_mutation_checks_use_stable_probes_and_error_empty_never_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = generate_qualification_repository(tmp_path / "mutation-repository")
    repository = replace(repository, workloads=repository.workloads[:1])
    initialize_deterministic_git(repository.root)
    scope = resolve_repository_scope(repository.root)

    class ErrorRuntime:
        def __init__(self) -> None:
            self.calls: list[object] = []

        def synchronize(self, *, deadline: float) -> None:
            assert deadline > time.monotonic()

        def query(self, request: object, *, deadline: float) -> NavigationResult:
            assert deadline > time.monotonic()
            self.calls.append(request)
            return _empty_navigation_result(request)

    runtime = ErrorRuntime()
    errors: list[dict[str, str]] = []
    mutation_queries: list[GoldQuery] = []
    real_navigation_request = _navigation_request

    def capture_query(query: GoldQuery, query_scope: object) -> object:
        mutation_queries.append(query)
        return real_navigation_request(query, query_scope)

    monkeypatch.setattr(mutation, "_navigation_request", capture_query)
    stale, cycles, checks_measured, latencies = _mutate_and_measure(
        runtime,
        repository,
        scope,
        errors,
    )

    workload = repository.workloads[0]
    assert len(runtime.calls) == 5
    assert {request.path for request in runtime.calls} == {workload.probe_path}
    assert _query_id_list(mutation_queries) == [
        "mutation-000-create",
        "mutation-000-edit",
        "mutation-000-rename-new",
        "mutation-000-rename-old",
        "mutation-000-delete",
    ]
    assert _expected_location_counts_list(mutation_queries) == [1, 1, 1, 0, 0]
    assert _first_location_paths(mutation_queries) == [
        workload.created_path,
        workload.original_path,
        workload.renamed_path,
    ]
    assert stale == 5
    assert cycles == 1
    assert checks_measured == 5
    assert latencies == []
    assert errors == []


def test_mutation_measurement_uses_the_facade_as_its_only_synchronization_path(
    tmp_path: Path,
) -> None:
    repository = generate_qualification_repository(tmp_path / "facade-sync-repository")
    repository = replace(repository, workloads=repository.workloads[:1])
    initialize_deterministic_git(repository.root)
    scope = resolve_repository_scope(repository.root)
    delegate = BehavioralNavigationRuntime(repository, scope)

    class QueryOnlyRuntime:
        def query(self, request: object, *, deadline: float) -> NavigationResult:
            return delegate.query(request, deadline=deadline)

    errors: list[dict[str, str]] = []
    stale, cycles, checks_measured, latencies = _mutate_and_measure(
        QueryOnlyRuntime(),
        repository,
        scope,
        errors,
    )

    assert stale == 0
    assert cycles == 1
    assert checks_measured == 5
    assert len(latencies) == 5
    assert errors == []


def test_all_mutation_cycles_reset_shared_paths_before_all_250_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = generate_qualification_repository(tmp_path / "shared-mutation-repository")
    initialize_deterministic_git(repository.root)
    scope = resolve_repository_scope(repository.root)
    first = repository.workloads[0]
    original = repository.root / first.original_path
    renamed = repository.root / first.renamed_path
    created = repository.root / first.created_path
    probe = repository.root / first.probe_path
    original.write_bytes(b"# stale original\n")
    renamed.write_bytes(b"# stale renamed\n")
    created.write_bytes(b"# stale created\n")
    probe.write_bytes(b"# stale probe\n")

    originals = {workload.original_content: workload.workload_id for workload in repository.workloads}
    probes = {
        workload.baseline_probe_content: workload.workload_id
        for workload in repository.workloads
    }
    original_resets: list[str] = []
    probe_resets: list[str] = []
    real_write_bytes = Path.write_bytes

    def record_reset(path: Path, data: bytes) -> int:
        if path == original and data in originals:
            original_resets.append(originals[data])
        if path == probe and data in probes:
            probe_resets.append(probes[data])
        return real_write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", record_reset)
    runtime = BehavioralNavigationRuntime(repository, scope)
    errors: list[dict[str, str]] = []
    stale, cycles, checks_measured, latencies = _mutate_and_measure(
        runtime,
        repository,
        scope,
        errors,
    )

    expected_order = [workload.workload_id for workload in repository.workloads]
    assert original_resets == expected_order
    assert probe_resets == expected_order
    assert stale == 0
    assert cycles == 50
    assert checks_measured == 250
    assert len(runtime.calls) == 250
    assert len(latencies) == 250
    assert errors == []


def test_mutation_reset_failure_is_recorded_and_skips_the_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = generate_qualification_repository(tmp_path / "failed-reset-repository")
    repository = replace(repository, workloads=repository.workloads[:1])
    initialize_deterministic_git(repository.root)
    scope = resolve_repository_scope(repository.root)
    workload = repository.workloads[0]
    probe = repository.root / workload.probe_path
    real_write_bytes = Path.write_bytes

    def fail_probe_reset(path: Path, data: bytes) -> int:
        if path == probe and data == workload.baseline_probe_content:
            raise OSError("injected reset failure")
        return real_write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", fail_probe_reset)
    runtime = BehavioralNavigationRuntime(repository, scope)
    errors: list[dict[str, str]] = []
    _stale, cycles, checks_measured, latencies = _mutate_and_measure(
        runtime,
        repository,
        scope,
        errors,
    )

    assert cycles == 0
    assert checks_measured == 0
    assert runtime.calls == []
    assert latencies == []
    assert errors == [{"phase": "mutation_reset", "code": "OSError"}]


def test_raw_tool_tokens_cover_the_complete_normalized_result(tmp_path: Path) -> None:
    repository = generate_qualification_repository(tmp_path / "token-repository")
    initialize_deterministic_git(repository.root)
    scope = resolve_repository_scope(repository.root)
    query = repository.gold_queries[0]
    request = _navigation_request(query, scope)
    runtime = BehavioralNavigationRuntime(repository, scope)
    result = runtime.query(request, deadline=time.monotonic() + SHORT_TIMEOUT)
    provenance = (Provenance("lsp", "pyright", PYRIGHT_VERSION, "provider_reported"),)
    location = replace(
        result.locations[0],
        containing_symbol="QualifiedContainer",
        signature="def target(value: int) -> int",
        provenance=provenance,
    )
    result = replace(
        result,
        readiness="query_ready_with_complete_normalized_metadata",
        symbol=query.symbol,
        locations=(location,),
        hover="fully normalized hover metadata",
        provenance=provenance,
        warnings=("complete-normalized-warning",),
    )
    rendered = render_navigation(result)
    expected_raw = {
        "status": result.status.value,
        "requested_capability": result.requested_capability.value,
        "effective_capability": result.effective_capability.value,
        "provider": result.provider,
        "provider_version": result.provider_version,
        "repository_id": result.repository_id,
        "checkout_id": result.checkout_id,
        "workspace_revision_before": result.workspace_revision_before,
        "workspace_revision_after": result.workspace_revision_after,
        "document_version": result.document_version,
        "position_encoding": result.position_encoding.value,
        "readiness": result.readiness,
        "symbol": result.symbol,
        "total": result.total,
        "offset": result.offset,
        "limit": result.limit,
        "locations": [
            {
                "path": location.path,
                "range": {
                    "byte_start": location.range.byte_start,
                    "byte_end": location.range.byte_end,
                },
                "line": location.line,
                "character": location.character,
                "containing_symbol": location.containing_symbol,
                "signature": location.signature,
                "resolution": location.resolution.value,
                "provenance": [
                    {
                        "source": item.source,
                        "provider": item.provider,
                        "version": item.version,
                        "observation": item.observation,
                    }
                    for item in location.provenance
                ],
            }
        ],
        "diagnostics": [],
        "hover": result.hover,
        "resolution": result.resolution.value,
        "provenance": [
            {
                "source": item.source,
                "provider": item.provider,
                "version": item.version,
                "observation": item.observation,
            }
            for item in result.provenance
        ],
        "warnings": list(result.warnings),
    }
    record = _token_record(query, request, result, rendered)
    assert record["raw_tool_tokens"] == estimate_tokens(
        schema._canonical_json(expected_raw)
    )
