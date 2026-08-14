"""Measured qualification runner for the Python code-navigation facade."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import io
import json
import math
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tokenize
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_ROOT = ROOT / "scripts"
BENCHMARK_ROOT = Path(__file__).resolve().parent
for _path in (SCRIPTS_ROOT, BENCHMARK_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import windows_workspace as _windows_workspace  # noqa: E402
from code_intelligence import Capability, PositionEncoding  # noqa: E402
from code_navigation import (  # noqa: E402
    CodeNavigation,
    NavigationLocation,
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
    WORKLOAD_CATALOG_PATH,
    GoldLocation,
    GoldQuery,
    QualificationRepository,
    current_gold_sha256,
    current_source_manifest_sha256,
    generate_qualification_repository,
    workload_catalog_bytes,
)
from lsp_positions import SourceDocument  # noqa: E402
from lsp_process import _coordinator_has_ownership  # noqa: E402
from lsp_protocol import CancellationSource, RequestCancelled  # noqa: E402
from lsp_security import (  # noqa: E402
    normalize_provider_uri,
    read_repository_source_bytes,
)
from pyright_profile import PyrightIdentity, discover_pyright  # noqa: E402
from pyright_session import LspLocation, PyrightSession  # noqa: E402
from repository_scope import (  # noqa: E402
    RepositoryScope,
    resolve_repository_scope,
    sanitized_git_environment,
)
from workspace_revision import compute_workspace_revision  # noqa: E402

FIXTURE_MANIFEST = BENCHMARK_ROOT / "code-navigation-python-v1.json"
MANIFEST_SCHEMA = BENCHMARK_ROOT / "code-navigation-python-v1.schema.json"
GOLD_SCHEMA = BENCHMARK_ROOT / "code-navigation-python-gold-v1.schema.json"
REPORT_SCHEMA = BENCHMARK_ROOT / "code-navigation-python-report-v1.schema.json"

REPORT_SCHEMA_VERSION = "code-navigation-python-report/v1"
RUNNER_EVIDENCE_VERSION = "code-navigation-real/v1"
CRASH_CYCLES = 20
DEFAULT_LIMIT = 10
MAX_ESTIMATED_TOKENS = 1200
QUERY_TIMEOUT_SECONDS = 90.0
CLEANUP_TIMEOUT_SECONDS = 30.0
RUN_TIMEOUT_SECONDS = 13.0 * 60.0
PERFORMANCE_SAMPLES = 20
FRESHNESS_CHECKS_PER_CYCLE = 5
_OWNERSHIP_TIMEOUT_SECONDS = 0.05
_OWNERSHIP_POLL_SECONDS = 0.001
_DIRECT_VALIDATION_MAX_SOURCE_BYTES = 16 * 1024 * 1024
OPERATOR_MAX_SCANNED_ENTRIES = 10_000
OPERATOR_MAX_DEPTH = 32
OPERATOR_MAX_PYTHON_FILES = 20
OPERATOR_MAX_SOURCE_BYTES = 1024 * 1024
OPERATOR_READ_CHUNK_BYTES = 64 * 1024
_OWNERSHIP_SCENARIOS = (
    "normal_shutdown",
    "crash",
    "timeout",
    "cancellation",
)
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PYTHON_310_VERSION = re.compile(r"3\.10\.(?:0|[1-9][0-9]*)\Z")

GATE_THRESHOLDS: dict[str, float | int] = {
    "definition_accuracy": 0.99,
    "reference_f1": 0.95,
    "stale_answer_count": 0,
    "stale_result_rate": 0.0,
    "orphan_process_count": 0,
    "orphan_process_rate": 0.0,
    "recovery_rate": 1.0,
    "default_items": 10,
    "default_estimated_tokens": 1200,
    "warm_overhead_p95_ms": 20,
    "cold_readiness_seconds": 60,
    "client_rss_mib": 100,
}

_CORRECTNESS_GATES = (
    "definition_accuracy",
    "reference_f1",
    "stale_answer_count",
    "stale_result_rate",
    "orphan_process_count",
    "orphan_process_rate",
    "recovery_rate",
    "default_items",
    "default_estimated_tokens",
)
_QUALIFICATION_GATES = (
    *_CORRECTNESS_GATES,
    "warm_overhead_p95_ms",
    "cold_readiness_seconds",
    "client_rss_mib",
)


class FixtureIdentityError(RuntimeError):
    """Raised when generated source or gold differs from the checked-in pins."""


class QualifiedIdentityError(RuntimeError):
    """Raised when no exact, already-installed Pyright identity is available."""


class BenchmarkTimeoutError(TimeoutError):
    """Raised when the one absolute benchmark run budget is exhausted."""


class _CleanupProofError(RuntimeError):
    """Raised when zero retained ownership cannot be proven after one retry."""


class _OperatorTraversalError(RuntimeError):
    """Raised when the bounded operator traversal cannot complete safely."""


@dataclass(frozen=True, slots=True)
class BenchmarkDependencies:
    """Injectable orchestration boundaries used by fast behavioral tests."""

    discover_identity: Callable[[RepositoryScope, Path, float], object]
    runtime_factory: Callable[[QualificationRepository, RepositoryScope, object, Path], object]
    operator_probe: Callable[[Path, Path, float], dict[str, object]] | None = None
    monotonic: Callable[[], float] = time.monotonic


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class _SchemaViolation(ValueError):
    pass


def _json_equal(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right


def _schema_type_matches(value: object, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return _finite_number(value)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    raise _SchemaViolation("unsupported schema type")


def _schema_accepts(value: object, schema: object, root: Mapping[str, object]) -> bool:
    try:
        _validate_schema_node(value, schema, root)
    except _SchemaViolation:
        return False
    return True


def _validate_schema_node(
    value: object,
    schema: object,
    root: Mapping[str, object],
) -> None:
    if not isinstance(schema, dict):
        raise _SchemaViolation("schema node must be an object")
    reference = schema.get("$ref")
    if reference is not None:
        if not isinstance(reference, str) or not reference.startswith("#/"):
            raise _SchemaViolation("only local schema references are supported")
        target: object = root
        for component in reference[2:].split("/"):
            if not isinstance(target, dict) or component not in target:
                raise _SchemaViolation("schema reference is unresolved")
            target = target[component]
        _validate_schema_node(value, target, root)
        return

    expected_type = schema.get("type")
    if expected_type is not None:
        expected_types = [expected_type] if isinstance(expected_type, str) else expected_type
        if not isinstance(expected_types, list) or not expected_types or not all(
            isinstance(item, str) for item in expected_types
        ):
            raise _SchemaViolation("schema type is invalid")
        if not any(_schema_type_matches(value, item) for item in expected_types):
            raise _SchemaViolation("value has the wrong type")

    if "const" in schema and not _json_equal(value, schema["const"]):
        raise _SchemaViolation("value differs from schema const")
    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, list) or not any(_json_equal(value, item) for item in choices):
            raise _SchemaViolation("value is outside schema enum")

    if isinstance(value, dict):
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise _SchemaViolation("schema required list is invalid")
        if not isinstance(properties, dict):
            raise _SchemaViolation("schema properties are invalid")
        if any(key not in value for key in required):
            raise _SchemaViolation("required property is absent")
        if schema.get("additionalProperties") is False and any(
            key not in properties for key in value
        ):
            raise _SchemaViolation("additional property is forbidden")
        for key, child_schema in properties.items():
            if key in value:
                _validate_schema_node(value[key], child_schema, root)

    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        maximum_items = schema.get("maxItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            raise _SchemaViolation("array is too short")
        if isinstance(maximum_items, int) and len(value) > maximum_items:
            raise _SchemaViolation("array is too long")
        if schema.get("uniqueItems") is True:
            encoded = [_canonical_json(item) for item in value]
            if len(encoded) != len(set(encoded)):
                raise _SchemaViolation("array items are not unique")
        if "items" in schema:
            for item in value:
                _validate_schema_node(item, schema["items"], root)

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            raise _SchemaViolation("string is too short")
        pattern = schema.get("pattern")
        if pattern is not None:
            if not isinstance(pattern, str) or re.search(pattern, value) is None:
                raise _SchemaViolation("string does not match schema pattern")

    if _finite_number(value):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if _finite_number(minimum) and float(value) < float(minimum):
            raise _SchemaViolation("number is below schema minimum")
        if _finite_number(maximum) and float(value) > float(maximum):
            raise _SchemaViolation("number is above schema maximum")

    one_of = schema.get("oneOf")
    if one_of is not None:
        if not isinstance(one_of, list) or sum(
            _schema_accepts(value, candidate, root) for candidate in one_of
        ) != 1:
            raise _SchemaViolation("value does not match exactly one schema branch")
    all_of = schema.get("allOf", [])
    if not isinstance(all_of, list):
        raise _SchemaViolation("schema allOf is invalid")
    for branch in all_of:
        if not isinstance(branch, dict):
            raise _SchemaViolation("schema allOf branch is invalid")
        condition = branch.get("if")
        if condition is None:
            _validate_schema_node(value, branch, root)
            continue
        selected = branch.get("then", {}) if _schema_accepts(value, condition, root) else branch.get(
            "else", {}
        )
        _validate_schema_node(value, selected, root)


def _validate_schema(value: object, schema_path: Path, label: str) -> None:
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} schema validation is unavailable") from exc
    try:
        if not isinstance(schema, dict):
            raise _SchemaViolation("schema root must be an object")
        _validate_schema_node(value, schema, schema)
    except (_SchemaViolation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} schema validation failed") from exc


def load_manifest(path: Path = FIXTURE_MANIFEST) -> dict[str, object]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("manifest must be a JSON object")
    return value


def validate_manifest(value: object) -> None:
    _validate_schema(value, MANIFEST_SCHEMA, "manifest")


def validate_gold(value: object) -> None:
    _validate_schema(value, GOLD_SCHEMA, "gold")


def validate_report(value: object) -> None:
    try:
        _canonical_json(value)
    except ValueError as exc:
        raise ValueError("report numbers must be finite") from exc
    _validate_schema(value, REPORT_SCHEMA, "report")


def precision_recall_f1(
    expected: Iterable[object],
    actual: Iterable[object],
) -> dict[str, int | float]:
    """Return set precision/recall/F1 with explicit empty-set semantics."""
    expected_set = set(expected)
    actual_set = set(actual)
    true_positive = len(expected_set & actual_set)
    false_positive = len(actual_set - expected_set)
    false_negative = len(expected_set - actual_set)
    precision = (
        true_positive / len(actual_set) if actual_set else (1.0 if not expected_set else 1.0)
    )
    recall = true_positive / len(expected_set) if expected_set else 1.0
    f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def nearest_rank_percentile(values: Sequence[float], percentile: float) -> float | None:
    """Return the nearest-rank percentile: sorted[ceil(p*n)-1]."""
    if not 0.0 < percentile <= 1.0 or not math.isfinite(percentile):
        raise ValueError("percentile must be finite and in (0, 1]")
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if not all(math.isfinite(value) for value in ordered):
        raise ValueError("percentile samples must be finite")
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _performance_queries(queries: Sequence[GoldQuery]) -> tuple[GoldQuery, ...]:
    by_capability = {
        capability: tuple(query for query in queries if query.capability == capability)
        for capability in ("definition", "references", "calls")
    }
    expected_counts = {
        "definition": DEFINITION_QUERIES,
        "references": REFERENCE_QUERIES,
        "calls": CALL_QUERIES,
    }
    if any(
        len(by_capability[capability]) != expected
        for capability, expected in expected_counts.items()
    ):
        raise ValueError("performance sample requires the complete gold query domain")

    def spread(capability: str, count: int) -> tuple[GoldQuery, ...]:
        candidates = by_capability[capability]
        denominator = count - 1
        return tuple(
            candidates[
                (index * (len(candidates) - 1) + denominator // 2) // denominator
            ]
            for index in range(count)
        )

    definitions = spread("definition", 10)
    references = spread("references", 5)
    calls = spread("calls", 5)
    selected = tuple(
        query
        for index in range(5)
        for query in (
            definitions[index * 2],
            references[index],
            calls[index],
            definitions[index * 2 + 1],
        )
    )
    if len(selected) != PERFORMANCE_SAMPLES or len({query.query_id for query in selected}) != len(
        selected
    ):
        raise AssertionError("performance query sample must contain 20 unique queries")
    return selected


def _measure_warm_performance_pair(
    runtime: object,
    request: object,
    *,
    next_deadline: Callable[[], float],
    perf_counter: Callable[[], float] = time.perf_counter,
) -> tuple[tuple[object, ...], tuple[object, ...], float, float]:
    direct_results: list[object] = []
    facade_results: list[object] = []
    direct_times: list[float] = []
    facade_times: list[float] = []
    for direct_first in (True, False):
        operations = ("direct", "facade") if direct_first else ("facade", "direct")
        for operation in operations:
            started = perf_counter()
            if operation == "direct":
                result = runtime.direct_query(request, deadline=next_deadline())
                direct_results.append(result)
                direct_times.append((perf_counter() - started) * 1000.0)
            else:
                result = runtime.query(request, deadline=next_deadline())
                facade_results.append(result)
                facade_times.append((perf_counter() - started) * 1000.0)
    return (
        tuple(direct_results),
        tuple(facade_results),
        sum(direct_times) / len(direct_times),
        sum(facade_times) / len(facade_times),
    )


def _observed_expected_exception(
    operation: Callable[[], object],
    expected: type[BaseException],
) -> bool:
    try:
        operation()
    except expected:
        return True
    except Exception:
        return False
    return False


def _check_run_deadline(
    run_deadline: float,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> float:
    now = monotonic()
    if not math.isfinite(run_deadline) or now >= run_deadline:
        raise BenchmarkTimeoutError("benchmark run deadline exceeded")
    return now


def _operation_deadline(
    run_deadline: float,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    limit: float = QUERY_TIMEOUT_SECONDS,
) -> float:
    now = _check_run_deadline(run_deadline, monotonic=monotonic)
    return min(run_deadline, now + limit)


def _fresh_cleanup_deadline(
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> float:
    return monotonic() + CLEANUP_TIMEOUT_SECONDS


def initialize_deterministic_git(
    repository_root: Path,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    """Create and commit a deterministic local repository after generation."""
    root = Path(repository_root).resolve(strict=True)
    if not root.is_dir() or (root / ".git").exists():
        raise ValueError("qualification Git root must be a fresh generated directory")
    environment = sanitized_git_environment()
    for name in ("GIT_DEFAULT_HASH", "GIT_TEMPLATE_DIR"):
        environment.pop(name, None)
    environment.update(
        {
            "GIT_AUTHOR_NAME": "LLM Wiki Qualification",
            "GIT_AUTHOR_EMAIL": "qualification@llm-wiki.invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
            "GIT_COMMITTER_NAME": "LLM Wiki Qualification",
            "GIT_COMMITTER_EMAIL": "qualification@llm-wiki.invalid",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "TZ": "UTC",
        }
    )
    with tempfile.TemporaryDirectory(prefix="code-navigation-git-isolation-") as temporary:
        isolation = Path(temporary)
        template = isolation / "template"
        hooks = isolation / "hooks"
        config = isolation / "config"
        template.mkdir()
        hooks.mkdir()
        config.write_bytes(b"")
        environment.update(
            GIT_CONFIG_GLOBAL=str(config),
            GIT_CONFIG_SYSTEM=str(config),
            GIT_TEMPLATE_DIR=str(template),
        )
        command_prefix = [
            "git",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.safecrlf=false",
            "-c",
            "core.filemode=false",
            "-c",
            f"core.hooksPath={hooks}",
            "-c",
            f"core.attributesFile={os.devnull}",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
        ]

        def run(*arguments: str, capture: bool = False) -> str:
            timeout = 30.0
            if deadline is not None:
                operation_end = _operation_deadline(deadline, monotonic=monotonic)
                timeout = min(timeout, max(0.001, operation_end - monotonic()))
            try:
                result = subprocess.run(
                    [*command_prefix, *arguments],
                    cwd=root,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    check=False,
                    shell=False,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                if deadline is not None and monotonic() >= deadline:
                    raise BenchmarkTimeoutError("benchmark run deadline exceeded") from exc
                raise RuntimeError("deterministic qualification Git command timed out") from exc
            if result.returncode != 0:
                raise RuntimeError("deterministic qualification Git command failed")
            if deadline is not None:
                _check_run_deadline(deadline, monotonic=monotonic)
            return result.stdout.decode("ascii", errors="strict").strip() if capture else ""

        run(
            "init",
            "--quiet",
            "--object-format=sha1",
            "--initial-branch=qualification",
            f"--template={template}",
        )
        run("add", "--all")
        run(
            "commit",
            "--quiet",
            "--no-gpg-sign",
            "--no-verify",
            "-m",
            "qualification fixture",
        )
        commit = run("rev-parse", "--verify", "HEAD^{commit}", capture=True)
        if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
            raise RuntimeError("deterministic qualification Git commit is invalid")
        return commit


def verify_repository_identity(
    repository: QualificationRepository,
    manifest: Mapping[str, object],
) -> None:
    if repository.line_count != manifest.get("fixture_lines"):
        raise FixtureIdentityError("generated fixture line count does not match manifest")
    current_source_hash = current_source_manifest_sha256(repository.root)
    if (
        current_source_hash != repository.source_manifest_sha256
        or current_source_hash != manifest.get("expected_source_manifest_sha256")
    ):
        raise FixtureIdentityError("generated source manifest hash does not match manifest")
    if (repository.root / WORKLOAD_CATALOG_PATH).read_bytes() != workload_catalog_bytes(
        repository.workloads
    ):
        raise FixtureIdentityError("generated workload catalog does not match workloads")
    current_gold_hash = current_gold_sha256(repository)
    if current_gold_hash != repository.gold_sha256 or current_gold_hash != manifest.get(
        "expected_gold_sha256"
    ):
        raise FixtureIdentityError("generated gold hash does not match manifest")


def _require_qualified_identity(identity: object, manifest: Mapping[str, object]) -> None:
    checks = (
        getattr(identity, "status", None) == "qualified",
        getattr(identity, "qualified", None) is True,
        getattr(identity, "version", None) == manifest["pyright_version"],
        getattr(identity, "package_sha256", None) == manifest["pyright_package_sha256"],
        getattr(identity, "node_major", None) == manifest["node_major"],
        isinstance(getattr(identity, "node_version", None), str)
        and bool(getattr(identity, "node_version", None)),
        not tuple(getattr(identity, "degradation_codes", ())),
    )
    if not all(checks):
        raise QualifiedIdentityError(
            "an exact qualified Pyright package and Node identity is required"
        )


def _capability(value: str) -> Capability:
    return {
        "definition": Capability.DEFINITIONS,
        "references": Capability.REFERENCES,
        "calls": Capability.CALLS,
    }[value]


def _navigation_request(query: GoldQuery, scope: RepositoryScope) -> NavigationRequest:
    return NavigationRequest(
        scope,
        _capability(query.capability),
        query.path,
        query.line,
        query.character,
        0,
        DEFAULT_LIMIT,
        query.direction,
    )


def _expected_key(location: GoldLocation) -> tuple[object, ...]:
    return (
        location.path,
        location.line,
        location.character,
        location.byte_start,
        location.byte_end,
    )


def _actual_key(location: NavigationLocation) -> tuple[object, ...]:
    return (
        location.path,
        location.line,
        location.character,
        location.range.byte_start,
        location.range.byte_end,
    )


def _navigation_assertion_succeeds(
    result: NavigationResult,
    *,
    expected: set[tuple[object, ...]],
    actual: set[tuple[object, ...]],
    citations_current: bool,
) -> bool:
    accepted_statuses = (
        {NavigationStatus.OK, NavigationStatus.PARTIAL}
        if expected
        else {NavigationStatus.OK}
    )
    return result.status in accepted_statuses and actual == expected and citations_current


def _direct_results_are_exact(
    runtime: object,
    repository: QualificationRepository,
    scope: RepositoryScope,
    query: GoldQuery,
    results: Sequence[object],
    *,
    deadline: float,
) -> bool:
    encoding = getattr(runtime, "position_encoding", None)
    if not isinstance(encoding, PositionEncoding):
        return False
    expected = {_expected_key(location) for location in query.expected_locations}
    expected_hashes = {
        _expected_key(location): location.source_sha256
        for location in query.expected_locations
    }
    for result in results:
        if getattr(result, "coverage", None) != "provider_reported":
            return False
        locations = getattr(result, "locations", None)
        if not isinstance(locations, tuple):
            return False
        actual: set[tuple[object, ...]] = set()
        for location in locations:
            if time.monotonic() >= deadline or not isinstance(location, LspLocation):
                return False
            source = normalize_provider_uri(scope, location.uri)
            if source is None:
                return False
            try:
                content = read_repository_source_bytes(
                    scope,
                    source.relative_path,
                    max_bytes=_DIRECT_VALIDATION_MAX_SOURCE_BYTES,
                    deadline=deadline,
                )
                document = SourceDocument.from_bytes(source.relative_path, content)
                range_ = document.to_byte_range(location.range, encoding)
                line = location.range.start.line + 1
                line_start, _line_end = document.line_spans[location.range.start.line]
            except Exception:
                return False
            key = (
                source.relative_path,
                line,
                range_.byte_start - line_start,
                range_.byte_start,
                range_.byte_end,
            )
            if document.source_sha256 != expected_hashes.get(key):
                return False
            actual.add(key)
        if actual != expected:
            return False
    return True


def _current_citation(
    repository_root: Path,
    location: NavigationLocation,
    *,
    expected_sha256: str,
) -> bool:
    try:
        if _HEX_SHA256.fullmatch(expected_sha256) is None:
            return False
        root = repository_root.resolve(strict=True)
        path = (root / location.path).resolve(strict=True)
        path.relative_to(root)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            return False
        start = location.range.byte_start
        end = location.range.byte_end
        if not 0 <= start < end <= len(content):
            return False
        content[:start].decode("utf-8", errors="strict")
        content[start:end].decode("utf-8", errors="strict")
        line_start = content.rfind(b"\n", 0, start) + 1
        return (
            content.count(b"\n", 0, start) + 1 == location.line
            and start - line_start == location.character
        )
    except (OSError, UnicodeError, ValueError):
        return False


def _rendered_item_count(payload: Mapping[str, object]) -> int:
    groups = payload.get("groups", [])
    diagnostics = payload.get("diagnostics", [])
    locations = 0
    if isinstance(groups, list):
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("locations"), list):
                locations += len(group["locations"])
    return locations + (len(diagnostics) if isinstance(diagnostics, list) else 0)


def _token_record(
    query: GoldQuery,
    request: NavigationRequest,
    result: NavigationResult,
    rendered: Mapping[str, object],
) -> dict[str, object]:
    request_value = {
        "capability": request.capability.value,
        "path": request.path,
        "line": request.line,
        "character": request.character,
        "direction": request.direction,
        "offset": request.offset,
        "limit": request.limit,
    }

    def provenance_value(item: object) -> dict[str, str]:
        return {
            "source": item.source,
            "provider": item.provider,
            "version": item.version,
            "observation": item.observation,
        }

    def location_value(location: NavigationLocation) -> dict[str, object]:
        return {
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
            "provenance": [provenance_value(item) for item in location.provenance],
        }

    raw_value = {
        "status": result.status.value,
        "requested_capability": result.requested_capability.value,
        "effective_capability": (
            result.effective_capability.value if result.effective_capability is not None else None
        ),
        "provider": result.provider,
        "provider_version": result.provider_version,
        "repository_id": result.repository_id,
        "checkout_id": result.checkout_id,
        "workspace_revision_before": result.workspace_revision_before,
        "workspace_revision_after": result.workspace_revision_after,
        "document_version": result.document_version,
        "position_encoding": (
            result.position_encoding.value if result.position_encoding is not None else None
        ),
        "readiness": result.readiness,
        "symbol": result.symbol,
        "total": result.total,
        "offset": result.offset,
        "limit": result.limit,
        "locations": [location_value(location) for location in result.locations],
        "diagnostics": [
            {
                "path": diagnostic.path,
                "range": {
                    "byte_start": diagnostic.range.byte_start,
                    "byte_end": diagnostic.range.byte_end,
                },
                "severity": diagnostic.severity.value,
                "code": diagnostic.code,
                "message": diagnostic.message,
                "related": [location_value(location) for location in diagnostic.related],
                "provenance": [provenance_value(item) for item in diagnostic.provenance],
            }
            for diagnostic in result.diagnostics
        ],
        "hover": result.hover,
        "resolution": result.resolution.value,
        "provenance": [provenance_value(item) for item in result.provenance],
        "warnings": list(result.warnings),
    }
    return {
        "query_id": query.query_id,
        "uncached_input_tokens": estimate_tokens(_canonical_json(request_value)),
        "cache_read_tokens": 0,
        "raw_tool_tokens": estimate_tokens(_canonical_json(raw_value)),
        "output_tokens": estimate_tokens(_canonical_json(rendered)),
    }


def _query_position(content: bytes, byte_start: int) -> tuple[int, int, int]:
    line_start = content.rfind(b"\n", 0, byte_start) + 1
    prefix = content[line_start:byte_start]
    return (
        content.count(b"\n", 0, byte_start) + 1,
        len(prefix),
        len(prefix.decode("utf-8", errors="strict")),
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
    expected: tuple[GoldLocation, ...]
    if target_content is None:
        expected = ()
    else:
        declaration = target_content.find(needle)
        if declaration < 0:
            raise ValueError("mutation workload target symbol is missing")
        target_line, target_character, _target_codepoint = _query_position(
            target_content, declaration
        )
        expected = (
            GoldLocation(
                target_path,
                target_line,
                target_character,
                declaration,
                declaration + len(needle),
                hashlib.sha256(target_content).hexdigest(),
            ),
        )
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


def _mutate_and_measure(
    runtime: object,
    repository: QualificationRepository,
    scope: RepositoryScope,
    errors: list[dict[str, str]],
    *,
    run_deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[int, int, int, list[float]]:
    stale = 0
    cycles_measured = 0
    checks_measured = 0
    latencies: list[float] = []
    for workload in repository.workloads:
        original = repository.root / workload.original_path
        renamed = repository.root / workload.renamed_path
        created = repository.root / workload.created_path
        probe = repository.root / workload.probe_path
        try:
            if run_deadline is not None:
                _check_run_deadline(run_deadline, monotonic=monotonic)
            renamed.unlink(missing_ok=True)
            created.unlink(missing_ok=True)
            original.write_bytes(workload.original_content)
            probe.write_bytes(workload.baseline_probe_content)
            if run_deadline is not None:
                _check_run_deadline(run_deadline, monotonic=monotonic)
        except BenchmarkTimeoutError:
            raise
        except Exception as exc:  # benchmark evidence retains a closed code only
            stale += FRESHNESS_CHECKS_PER_CYCLE
            errors.append({"phase": "mutation_reset", "code": type(exc).__name__})
            continue
        steps = (
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
        cycle_ok = True
        for operation, mutate, query in steps:
            started = time.perf_counter()
            try:
                deadline = (
                    _operation_deadline(run_deadline, monotonic=monotonic)
                    if run_deadline is not None
                    else monotonic() + QUERY_TIMEOUT_SECONDS
                )
                mutate()
                if run_deadline is not None:
                    _check_run_deadline(run_deadline, monotonic=monotonic)
                result = runtime.query(
                    _navigation_request(query, scope),
                    deadline=deadline,
                )
                checks_measured += 1
                actual = {_actual_key(location) for location in result.locations}
                expected = {_expected_key(location) for location in query.expected_locations}
                expected_hashes = {
                    _expected_key(location): location.source_sha256
                    for location in query.expected_locations
                }
                citations = all(
                    _current_citation(
                        repository.root,
                        location,
                        expected_sha256=expected_hashes.get(
                            _actual_key(location),
                            "",
                        ),
                    )
                    for location in result.locations
                )
                fresh = _navigation_assertion_succeeds(
                    result,
                    expected=expected,
                    actual=actual,
                    citations_current=citations,
                )
                if not fresh:
                    stale += 1
                else:
                    latencies.append((time.perf_counter() - started) * 1000.0)
                if run_deadline is not None:
                    _check_run_deadline(run_deadline, monotonic=monotonic)
            except BenchmarkTimeoutError:
                raise
            except Exception as exc:  # benchmark evidence retains a closed code only
                stale += 1
                cycle_ok = False
                errors.append(
                    {
                        "phase": f"mutation_{operation}",
                        "code": type(exc).__name__,
                    }
                )
                if run_deadline is not None:
                    _check_run_deadline(run_deadline, monotonic=monotonic)
        if cycle_ok:
            cycles_measured += 1
    return stale, cycles_measured, checks_measured, latencies


class _RealNavigationRuntime:
    """Own the real PyrightSession and CodeNavigation lifecycle."""

    def __init__(
        self,
        repository: QualificationRepository,
        scope: RepositoryScope,
        identity: PyrightIdentity,
        state_root: Path,
    ) -> None:
        self.repository = repository
        self.scope = scope
        self.identity = identity
        self.state_root = state_root
        self._session: PyrightSession
        self._navigation: CodeNavigation
        self._last_request: NavigationRequest | None = None
        self._cleanup_failed = False
        self._open()

    def _open(self) -> None:
        self._session = PyrightSession(
            self.scope,
            self.identity,
            state_root=self.state_root,
        )
        self._navigation = CodeNavigation(
            self.scope,
            self._session,
            self.identity,
        )

    def query(self, request: NavigationRequest, *, deadline: float) -> NavigationResult:
        self._last_request = request
        return self._navigation.query(request, deadline=deadline)

    def synchronize(self, *, deadline: float) -> None:
        revision = compute_workspace_revision(self.scope, deadline=deadline)
        self._session.synchronize(revision, deadline=deadline)

    def direct_query(self, request: NavigationRequest, *, deadline: float) -> object:
        revision = compute_workspace_revision(self.scope, deadline=deadline)
        self._session.synchronize(revision, deadline=deadline)
        content = (self.repository.root / request.path).read_bytes()
        anchor = SourceDocument.from_bytes(request.path, content).validate_anchor(
            line=request.line,
            character=request.character,
        )
        if request.capability is Capability.DEFINITIONS:
            return self._session.definition(anchor, deadline=deadline)
        if request.capability is Capability.REFERENCES:
            return self._session.references(anchor, deadline=deadline)
        if request.capability is Capability.CALLS:
            if request.direction == "incoming":
                return self._session.incoming_calls(anchor, deadline=deadline)
            return self._session.outgoing_calls(anchor, deadline=deadline)
        raise ValueError("unsupported direct qualification capability")

    @property
    def position_encoding(self) -> PositionEncoding | None:
        return self._session.position_encoding

    def _close_and_count(self, deadline: float) -> int:
        process = self._session._process
        coordinator = process._coordinator if process is not None else None
        popen = process.process if process is not None else None
        lease = process.owner_root / "lease.json" if process is not None else None
        self._navigation.close(deadline=deadline)
        return int(
            (popen is not None and popen.poll() is None)
            or (coordinator is not None and _coordinator_has_ownership(coordinator))
            or (lease is not None and lease.exists())
        )

    @property
    def cleanup_failed(self) -> bool:
        return bool(getattr(self, "_cleanup_failed", False))

    def _retry_retained_cleanup(self) -> None:
        try:
            retried = self._close_and_count(_fresh_cleanup_deadline())
        except Exception:
            return
        if isinstance(retried, bool) or retried not in {0, 1}:
            return

    def _reset(self, deadline: float) -> int:
        if self.cleanup_failed:
            raise _CleanupProofError("runtime cleanup is terminal")
        try:
            orphan = self._close_and_count(deadline)
        except Exception as exc:
            self._cleanup_failed = True
            self._retry_retained_cleanup()
            raise _CleanupProofError("runtime cleanup could not be proven") from exc
        if isinstance(orphan, bool) or orphan not in {0, 1}:
            self._cleanup_failed = True
            self._retry_retained_cleanup()
            raise _CleanupProofError("runtime cleanup returned invalid evidence")
        if orphan:
            self._cleanup_failed = True
            self._retry_retained_cleanup()
            raise _CleanupProofError("runtime cleanup reported retained ownership")
        self._open()
        return 0

    def crash_and_recover(
        self,
        request: NavigationRequest,
        *,
        deadline: float,
    ) -> NavigationResult | None:
        if self.cleanup_failed:
            return None
        self.query(request, deadline=deadline)
        process = self._session._process
        if process is None:
            return None
        try:
            process.process.kill()
            return self._query_after_crash(request, deadline)
        finally:
            self._reset(deadline)

    def _query_after_crash(
        self,
        request: NavigationRequest,
        deadline: float,
    ) -> NavigationResult:
        retried_oserror = False
        while True:
            try:
                recovered = self.query(request, deadline=deadline)
            except OSError:
                if retried_oserror or time.monotonic() >= deadline:
                    raise
                retried_oserror = True
                continue
            if not (
                isinstance(recovered, NavigationResult)
                and recovered.status is NavigationStatus.NOT_READY
                and time.monotonic() < deadline
            ):
                return recovered

    def _prepare_process(self, deadline: float):
        request = self._last_request
        if request is None:
            raise RuntimeError("ownership probe requires a prior navigation request")
        self.query(request, deadline=deadline)
        process = self._session._process
        if process is None:
            raise RuntimeError("ownership probe has no live process")
        return request, process

    def _observe_inflight_interruption(
        self,
        process: object,
        scenario: str,
        deadline: float,
    ) -> None:
        method = "workspace/symbol"
        protocol = process.protocol
        baseline_sequence, _baseline_method = protocol._sent_request_evidence()
        source = CancellationSource()
        now = time.monotonic()
        request_deadline = (
            min(deadline, now + _OWNERSHIP_TIMEOUT_SECONDS)
            if scenario == "timeout"
            else deadline
        )
        if now >= request_deadline:
            raise TimeoutError("ownership probe deadline expired")

        completed = threading.Event()
        outcome: list[tuple[str, object]] = []

        def request_workspace_symbols() -> None:
            try:
                result = process.request(
                    method,
                    {"query": "__llm_wiki_ownership_probe_no_match__"},
                    deadline=request_deadline,
                    cancellation=source.token,
                )
            except BaseException as exc:
                outcome.append(("error", exc))
            else:
                outcome.append(("result", result))
            finally:
                completed.set()

        worker = threading.Thread(
            target=request_workspace_symbols,
            name=f"code-navigation-ownership-{scenario}",
            daemon=True,
        )
        worker.start()
        dispatched = False
        while time.monotonic() < deadline:
            sent_sequence, sent_method = protocol._sent_request_evidence()
            if sent_sequence > baseline_sequence and sent_method == method:
                dispatched = True
                break
            wait_for = min(
                _OWNERSHIP_POLL_SECONDS,
                max(0.0, deadline - time.monotonic()),
            )
            if completed.is_set():
                time.sleep(wait_for)
            else:
                completed.wait(wait_for)

        if dispatched and scenario == "cancellation":
            source.cancel()

        if not completed.wait(max(0.0, deadline - time.monotonic())):
            self._cleanup_failed = True
            raise _CleanupProofError("ownership probe request did not terminate")
        worker.join(max(0.0, deadline - time.monotonic()))
        if worker.is_alive():
            self._cleanup_failed = True
            raise _CleanupProofError("ownership probe request owner did not stop")
        if not dispatched:
            raise RuntimeError("ownership probe request was not sent")
        if len(outcome) != 1 or outcome[0][0] != "error":
            raise RuntimeError("ownership probe did not reach the expected terminal")
        error = outcome[0][1]
        expected = TimeoutError if scenario == "timeout" else RequestCancelled
        if not isinstance(error, expected):
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise error
            raise RuntimeError("ownership probe reached the wrong terminal") from error

    def ownership_checks(self, *, deadline: float) -> dict[str, dict[str, int | bool | None]]:
        outcomes: dict[str, dict[str, int | bool | None]] = {
            scenario: {"available": False, "orphan_count": None}
            for scenario in _OWNERSHIP_SCENARIOS
        }
        for scenario in _OWNERSHIP_SCENARIOS:
            if self.cleanup_failed:
                break
            available = True
            orphan: int | None = None
            try:
                request, process = self._prepare_process(deadline)
                if scenario == "crash":
                    process.process.kill()
                    self._query_after_crash(request, deadline)
                elif scenario == "timeout":
                    self._observe_inflight_interruption(process, scenario, deadline)
                elif scenario == "cancellation":
                    self._observe_inflight_interruption(process, scenario, deadline)
                orphan = self._reset(deadline)
            except Exception:
                available = False
                if not self.cleanup_failed:
                    with contextlib.suppress(Exception):
                        self._reset(deadline)
            outcomes[scenario] = {
                "available": available,
                "orphan_count": orphan,
            }
        return outcomes

    def close(self, *, deadline: float) -> int:
        return self._close_and_count(deadline)


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


def _peak_rss() -> tuple[float | None, str]:
    if os.name == "nt":
        try:
            from ctypes import wintypes

            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            get_current_process = kernel32.GetCurrentProcess
            get_current_process.argtypes = []
            get_current_process.restype = wintypes.HANDLE
            get_process_memory_info = psapi.GetProcessMemoryInfo
            get_process_memory_info.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(ProcessMemoryCounters),
                wintypes.DWORD,
            ]
            get_process_memory_info.restype = wintypes.BOOL
            handle = get_current_process()
            if not get_process_memory_info(handle, ctypes.byref(counters), counters.cb):
                raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
            return (
                counters.PeakWorkingSetSize / (1024.0 * 1024.0),
                "measured-windows-peak-working-set",
            )
        except (AttributeError, OSError, TypeError, ValueError):
            pass
    else:
        try:
            import resource

            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            multiplier = 1 if sys.platform == "darwin" else 1024
            return (
                usage * multiplier / (1024.0 * 1024.0),
                "measured-posix-ru-maxrss",
            )
        except (ImportError, OSError, ValueError):
            pass
    return None, "unavailable"


def _ram_bytes() -> int | None:
    if os.name == "nt":
        try:

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.total_physical)
        except (AttributeError, OSError, ValueError):
            return None
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int(pages * page_size)
    except (AttributeError, OSError, ValueError):
        return None


def _ram_class() -> str:
    total = _ram_bytes()
    if total is None:
        return "unavailable"
    gib = total / (1024**3)
    for lower, upper in ((0, 4), (4, 8), (8, 16), (16, 32), (32, 64), (64, 128)):
        if lower <= gib < upper:
            return f"{lower}-{upper - 1} GiB"
    return "128+ GiB"


def _environment() -> dict[str, object]:
    return {
        "os": platform.system() or os.name,
        "os_version": platform.version() or platform.release() or "unavailable",
        "architecture": platform.machine() or "unavailable",
        "cpu_model": platform.processor() or platform.machine() or "unavailable",
        "cpu_core_count": max(1, os.cpu_count() or 1),
        "ram_class": _ram_class(),
    }


def _operator_file_state(info: object) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        info.st_size,
        info.st_mtime_ns,
        0 if os.name == "nt" else info.st_ctime_ns,
    )


def _operator_link_or_reparse(info: object) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _validate_operator_source_chain(path: Path, root: Path, deadline: float) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PermissionError("operator source is outside operator root") from exc
    current = root
    while True:
        _check_run_deadline(deadline)
        info = current.stat(follow_symlinks=False)
        if _operator_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise PermissionError("operator source parent must be a regular directory")
        if current == path.parent:
            return
        relative = path.parent.relative_to(current)
        current /= relative.parts[0]


def _read_operator_source(path: Path, operator_root: Path, *, deadline: float) -> bytes:
    source = Path(os.path.abspath(os.fspath(path)))
    root = Path(os.path.abspath(os.fspath(operator_root)))
    _validate_operator_source_chain(source, root, deadline)
    _check_run_deadline(deadline)
    before = source.stat(follow_symlinks=False)
    if _operator_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise PermissionError("operator source must be a regular file")
    if before.st_size > OPERATOR_MAX_SOURCE_BYTES:
        raise ValueError("operator source exceeds the byte limit")
    expected_state = _operator_file_state(before)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    windows_handle: int | None = None
    windows_state: tuple[object, ...] | None = None
    _check_run_deadline(deadline)
    if os.name == "nt":
        import msvcrt

        windows_handle = _windows_workspace.open_exclusive_readonly_source_file(source)
        try:
            windows_state = (
                _windows_workspace.identity(windows_handle, directory=False),
                _windows_workspace.file_size(windows_handle),
                _windows_workspace.file_modified_time_ns(windows_handle),
            )
            descriptor = msvcrt.open_osfhandle(windows_handle, flags)
        except BaseException:
            _windows_workspace.close_handle(windows_handle)
            raise
    else:
        descriptor = os.open(
            source,
            flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    try:
        _check_run_deadline(deadline)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _operator_file_state(opened) != expected_state:
            raise PermissionError("operator source changed before open")
        chunks: list[bytes] = []
        total = 0
        while total <= OPERATOR_MAX_SOURCE_BYTES:
            _check_run_deadline(deadline)
            chunk = os.read(
                descriptor,
                min(
                    OPERATOR_READ_CHUNK_BYTES,
                    OPERATOR_MAX_SOURCE_BYTES + 1 - total,
                ),
            )
            _check_run_deadline(deadline)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > OPERATOR_MAX_SOURCE_BYTES:
            raise ValueError("operator source exceeds the byte limit")
        after = os.fstat(descriptor)
        if _operator_file_state(after) != expected_state:
            raise PermissionError("operator source changed during read")
        if windows_handle is not None:
            after_windows_state = (
                _windows_workspace.identity(windows_handle, directory=False),
                _windows_workspace.file_size(windows_handle),
                _windows_workspace.file_modified_time_ns(windows_handle),
            )
            if after_windows_state != windows_state:
                raise PermissionError("operator source changed during read")
        _check_run_deadline(deadline)
        current = source.stat(follow_symlinks=False)
        if _operator_link_or_reparse(current) or _operator_file_state(current) != expected_state:
            raise PermissionError("operator source was replaced during read")
        _validate_operator_source_chain(source, root, deadline)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _operator_definition(
    path: Path,
    root: Path,
    *,
    operator_root: Path,
    deadline: float,
) -> tuple[str, int, int] | None:
    content = _read_operator_source(path, operator_root, deadline=deadline)
    tokens = tokenize.tokenize(io.BytesIO(content).readline)
    expect_name = False
    for token in tokens:
        if token.type == tokenize.NAME and token.string in {"def", "class"}:
            expect_name = True
            continue
        if expect_name and token.type == tokenize.NAME:
            line_bytes = content.splitlines()[token.start[0] - 1]
            prefix = line_bytes.decode("utf-8")[: token.start[1]].encode("utf-8")
            return (
                path.relative_to(root).as_posix(),
                token.start[0],
                len(prefix),
            )
        if expect_name and token.type not in {tokenize.NL, tokenize.INDENT}:
            expect_name = False
    return None


def _operator_python_files(
    operator_root: Path,
    *,
    deadline: float,
) -> tuple[Path, list[Path]]:
    requested = Path(operator_root)
    try:
        requested_metadata = requested.stat(follow_symlinks=False)
    except OSError as exc:
        raise _OperatorTraversalError("operator root is unavailable") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        requested.is_symlink()
        or getattr(requested_metadata, "st_file_attributes", 0) & reparse_flag
        or not stat.S_ISDIR(requested_metadata.st_mode)
    ):
        raise _OperatorTraversalError("operator root is not a regular directory")
    root = requested.resolve(strict=True)
    files: list[Path] = []
    stack = [(root, 0)]
    visited: set[tuple[int, int]] = set()
    scanned_entries = 0
    while stack and len(files) < OPERATOR_MAX_PYTHON_FILES:
        _check_run_deadline(deadline)
        current, depth = stack.pop()
        try:
            current_metadata = current.stat(follow_symlinks=False)
        except OSError as exc:
            raise _OperatorTraversalError("operator directory is unavailable") from exc
        identity = (current_metadata.st_dev, current_metadata.st_ino)
        if identity in visited:
            continue
        visited.add(identity)
        try:
            with os.scandir(current) as iterator:
                entries = []
                for entry in iterator:
                    _check_run_deadline(deadline)
                    scanned_entries += 1
                    if scanned_entries > OPERATOR_MAX_SCANNED_ENTRIES:
                        raise _OperatorTraversalError(
                            "operator traversal exceeds the entry limit"
                        )
                    entries.append(entry)
        except OSError as exc:
            raise _OperatorTraversalError("operator directory cannot be scanned") from exc

        child_directories: list[tuple[Path, int]] = []
        for entry in sorted(entries, key=lambda item: item.name):
            _check_run_deadline(deadline)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise _OperatorTraversalError("operator entry cannot be inspected") from exc
            if entry.is_symlink() or getattr(metadata, "st_file_attributes", 0) & reparse_flag:
                continue
            path = Path(entry.path)
            if stat.S_ISDIR(metadata.st_mode):
                child_depth = depth + 1
                if child_depth > OPERATOR_MAX_DEPTH:
                    raise _OperatorTraversalError("operator traversal exceeds the depth limit")
                child_directories.append((path, child_depth))
                continue
            if not stat.S_ISREG(metadata.st_mode) or path.suffix != ".py":
                continue
            try:
                candidate = path.resolve(strict=True)
                candidate.relative_to(root)
            except (OSError, ValueError) as exc:
                raise _OperatorTraversalError("operator file escaped its root") from exc
            files.append(candidate)
            if len(files) >= OPERATOR_MAX_PYTHON_FILES:
                break
        stack.extend(reversed(child_directories))
    return root, files


def _probe_operator_corpus(
    operator_root: Path,
    state_root: Path,
    deadline: float,
) -> dict[str, object]:
    files: list[Path] = []
    attempts = 0
    successes = 0
    errors = 0
    available = True
    navigation: CodeNavigation | None = None
    try:
        root, files = _operator_python_files(operator_root, deadline=deadline)
        _check_run_deadline(deadline)
        scope = resolve_repository_scope(root)
        discovery_deadline = _operation_deadline(deadline)
        identity = discover_pyright(
            scope,
            state_root=state_root,
            deadline=discovery_deadline,
        )
        _check_run_deadline(deadline)
        _require_qualified_identity(identity, load_manifest())
        session = PyrightSession(scope, identity, state_root=state_root)
        navigation = CodeNavigation(scope, session, identity)
        checkout = Path(scope.checkout_root)
        for path in files:
            try:
                query_deadline = _operation_deadline(deadline)
                definition = _operator_definition(
                    path,
                    checkout,
                    operator_root=root,
                    deadline=query_deadline,
                )
                _check_run_deadline(deadline)
                if definition is None:
                    continue
                relative, line, character = definition
                attempts += 1
                result = navigation.query(
                    NavigationRequest(
                        scope,
                        Capability.DEFINITIONS,
                        relative,
                        line,
                        character,
                    ),
                    deadline=query_deadline,
                )
                if result.status in {NavigationStatus.OK, NavigationStatus.PARTIAL}:
                    successes += 1
                _check_run_deadline(deadline)
            except BenchmarkTimeoutError:
                raise
            except Exception:
                errors += 1
                available = False
                break
    except BenchmarkTimeoutError:
        raise
    except Exception:
        errors += 1
        available = False
    finally:
        if navigation is not None:
            try:
                navigation.close(deadline=_fresh_cleanup_deadline())
            except Exception:
                errors += 1
                available = False
                try:
                    navigation.close(deadline=_fresh_cleanup_deadline())
                except Exception:
                    errors += 1
    return {
        "available": available,
        "python_files": len(files),
        "queries_attempted": attempts,
        "queries_succeeded": successes,
        "errors": errors,
    }


def _operator_metrics(value: object) -> dict[str, object]:
    keys = {
        "available",
        "python_files",
        "queries_attempted",
        "queries_succeeded",
        "errors",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("operator corpus probe returned a non-aggregate result")
    if not isinstance(value["available"], bool) or any(
        isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] < 0
        for key in keys - {"available"}
    ):
        raise ValueError("operator corpus aggregate metrics are invalid")
    return dict(value)


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
    monotonic = dependencies.monotonic
    run_deadline = monotonic() + RUN_TIMEOUT_SECONDS
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
    commit = initialize_deterministic_git(
        repository.root,
        deadline=run_deadline,
        monotonic=monotonic,
    )
    _check_run_deadline(run_deadline, monotonic=monotonic)
    scope = resolve_repository_scope(repository.root)
    _check_run_deadline(run_deadline, monotonic=monotonic)
    if scope.git_commit != commit:
        raise FixtureIdentityError("resolved Git commit does not match generated commit")
    identity = dependencies.discover_identity(
        scope,
        state_root,
        _operation_deadline(run_deadline, monotonic=monotonic),
    )
    _check_run_deadline(run_deadline, monotonic=monotonic)
    _require_qualified_identity(identity, manifest_value)
    runtime = dependencies.runtime_factory(repository, scope, identity, state_root)
    _check_run_deadline(run_deadline, monotonic=monotonic)

    definition_attempted = 0
    definition_exact = 0
    reference_expected: set[tuple[object, ...]] = set()
    reference_actual: set[tuple[object, ...]] = set()
    call_expected: set[tuple[object, ...]] = set()
    call_actual: set[tuple[object, ...]] = set()
    query_attempts = 0
    tasks_solved = 0
    citation_total = 0
    citation_correct = 0
    token_tasks: list[dict[str, object]] = []
    default_items = 0
    default_tokens = 0
    errors: list[dict[str, str]] = []
    first_request: NavigationRequest | None = None
    cold_readiness_seconds: float | None = None
    ownership: dict[str, dict[str, int | bool | None]] = {
        scenario: {"available": False, "orphan_count": None} for scenario in _OWNERSHIP_SCENARIOS
    }

    try:
        for index, query in enumerate(repository.gold_queries):
            request = _navigation_request(query, scope)
            if first_request is None:
                first_request = request
            started = time.perf_counter()
            operation_deadline = _operation_deadline(
                run_deadline,
                monotonic=monotonic,
            )
            query_attempts += 1
            try:
                result = runtime.query(
                    request,
                    deadline=operation_deadline,
                )
            except BenchmarkTimeoutError:
                raise
            except Exception as exc:
                errors.append({"phase": "gold_query", "code": type(exc).__name__})
                result = None
            _check_run_deadline(run_deadline, monotonic=monotonic)
            elapsed = time.perf_counter() - started
            expected = {_expected_key(location) for location in query.expected_locations}
            expected_hashes = {
                _expected_key(location): location.source_sha256
                for location in query.expected_locations
            }
            result_successful = result is not None and result.status in {
                NavigationStatus.OK,
                NavigationStatus.PARTIAL,
            }
            actual = (
                {_actual_key(location) for location in result.locations}
                if result_successful
                else set()
            )
            if query.capability == "definition":
                definition_attempted += 1
                if actual == expected:
                    definition_exact += 1
            elif query.capability == "references":
                reference_expected.update((query.query_id, *item) for item in expected)
                reference_actual.update((query.query_id, *item) for item in actual)
            else:
                call_expected.update((query.query_id, *item) for item in expected)
                call_actual.update((query.query_id, *item) for item in actual)
            if result is None:
                continue
            citations = [
                _current_citation(
                    repository.root,
                    location,
                    expected_sha256=expected_hashes.get(_actual_key(location), ""),
                )
                for location in result.locations
            ]
            citation_total += len(citations)
            citation_correct += sum(citations)
            solved = result_successful and _navigation_assertion_succeeds(
                result,
                expected=expected,
                actual=actual,
                citations_current=all(citations),
            )
            if index == 0 and mode == "qualification" and solved:
                cold_readiness_seconds = elapsed
            rendered = render_navigation(
                result,
                offset=0,
                limit=DEFAULT_LIMIT,
            )
            rendered_tokens = estimate_tokens(_canonical_json(rendered))
            default_items = max(default_items, _rendered_item_count(rendered))
            default_tokens = max(default_tokens, rendered_tokens)
            if solved:
                tasks_solved += 1
                token_tasks.append(_token_record(query, request, result, rendered))
            _check_run_deadline(run_deadline, monotonic=monotonic)

        warm_facade: list[float] = []
        direct_pyright: list[float] = []
        overhead: list[float] = []
        if mode == "qualification":
            for query in _performance_queries(repository.gold_queries):
                request = _navigation_request(query, scope)
                try:
                    direct_results, facade_results, direct_ms, facade_ms = (
                        _measure_warm_performance_pair(
                            runtime,
                            request,
                            next_deadline=lambda: _operation_deadline(
                                run_deadline,
                                monotonic=monotonic,
                            ),
                        )
                    )
                    _check_run_deadline(run_deadline, monotonic=monotonic)
                except BenchmarkTimeoutError:
                    raise
                except Exception as exc:
                    errors.append(
                        {
                            "phase": "performance_sample",
                            "code": type(exc).__name__,
                        }
                    )
                    _check_run_deadline(run_deadline, monotonic=monotonic)
                    continue
                if not _direct_results_are_exact(
                    runtime,
                    repository,
                    scope,
                    query,
                    direct_results,
                    deadline=_operation_deadline(
                        run_deadline,
                        monotonic=monotonic,
                    ),
                ):
                    errors.append(
                        {
                            "phase": "performance_direct",
                            "code": "UnsuccessfulDirectResult",
                        }
                    )
                    continue
                expected = {_expected_key(location) for location in query.expected_locations}
                expected_hashes = {
                    _expected_key(location): location.source_sha256
                    for location in query.expected_locations
                }
                facade_valid = True
                for facade_result in facade_results:
                    actual = {_actual_key(location) for location in facade_result.locations}
                    citations = all(
                        _current_citation(
                            repository.root,
                            location,
                            expected_sha256=expected_hashes.get(
                                _actual_key(location),
                                "",
                            ),
                        )
                        for location in facade_result.locations
                    )
                    if not _navigation_assertion_succeeds(
                        facade_result,
                        expected=expected,
                        actual=actual,
                        citations_current=citations,
                    ):
                        facade_valid = False
                        break
                if not facade_valid:
                    errors.append(
                        {
                            "phase": "performance_facade",
                            "code": "UnsuccessfulNavigationResult",
                        }
                    )
                    continue
                direct_pyright.append(direct_ms)
                warm_facade.append(facade_ms)
                overhead.append(facade_ms - direct_ms)

        (
            stale,
            mutation_cycles,
            freshness_checks_measured,
            freshness_latencies,
        ) = _mutate_and_measure(
            runtime,
            repository,
            scope,
            errors,
            run_deadline=run_deadline,
            monotonic=monotonic,
        )

        crash_attempts = 0
        crash_recoveries = 0
        crash_query = repository.gold_queries[-1]
        crash_request = _navigation_request(crash_query, scope)
        crash_expected = {_expected_key(location) for location in crash_query.expected_locations}
        crash_hashes = {
            _expected_key(location): location.source_sha256
            for location in crash_query.expected_locations
        }
        if first_request is not None:
            for _index in range(CRASH_CYCLES):
                crash_deadline = _operation_deadline(
                    run_deadline,
                    monotonic=monotonic,
                )
                crash_attempts += 1
                try:
                    recovered = runtime.crash_and_recover(
                        crash_request,
                        deadline=crash_deadline,
                    )
                    recovered_locations = (
                        set()
                        if not isinstance(recovered, NavigationResult)
                        else {_actual_key(location) for location in recovered.locations}
                    )
                    recovered_citations = isinstance(recovered, NavigationResult) and all(
                        _current_citation(
                            repository.root,
                            location,
                            expected_sha256=crash_hashes.get(
                                _actual_key(location),
                                "",
                            ),
                        )
                        for location in recovered.locations
                    )
                    if isinstance(recovered, NavigationResult) and _navigation_assertion_succeeds(
                        recovered,
                        expected=crash_expected,
                        actual=recovered_locations,
                        citations_current=recovered_citations,
                    ):
                        crash_recoveries += 1
                except BenchmarkTimeoutError:
                    raise
                except Exception as exc:
                    errors.append({"phase": "crash_recovery", "code": type(exc).__name__})
                _check_run_deadline(run_deadline, monotonic=monotonic)
                if getattr(runtime, "cleanup_failed", False):
                    errors.append({"phase": "cleanup", "code": "CleanupTerminal"})
                    break

        if not getattr(runtime, "cleanup_failed", False):
            try:
                ownership = runtime.ownership_checks(
                    deadline=_operation_deadline(
                        run_deadline,
                        monotonic=monotonic,
                    )
                )
                _check_run_deadline(run_deadline, monotonic=monotonic)
                if getattr(runtime, "cleanup_failed", False):
                    errors.append({"phase": "ownership", "code": "CleanupTerminal"})
            except BenchmarkTimeoutError:
                raise
            except Exception as exc:
                errors.append({"phase": "ownership", "code": type(exc).__name__})
                ownership = {
                    scenario: {"available": False, "orphan_count": None}
                    for scenario in _OWNERSHIP_SCENARIOS
                }
                _check_run_deadline(run_deadline, monotonic=monotonic)
    finally:
        final_incident = _close_runtime_with_retry(
            runtime,
            errors,
            monotonic=monotonic,
        )
        normal_shutdown = ownership.get("normal_shutdown")
        if final_incident is None:
            ownership["normal_shutdown"] = {
                "available": False,
                "orphan_count": None,
            }
        elif (
            isinstance(normal_shutdown, dict)
            and normal_shutdown.get("available") is True
            and isinstance(normal_shutdown.get("orphan_count"), int)
            and not isinstance(normal_shutdown.get("orphan_count"), bool)
            and normal_shutdown.get("orphan_count") in {0, 1}
        ):
            normal_shutdown["orphan_count"] = max(
                normal_shutdown["orphan_count"],
                final_incident,
            )

    ownership_checks = sum(
        isinstance(value, dict) and value.get("available") is True for value in ownership.values()
    )
    orphan_values = [
        value.get("orphan_count")
        for value in ownership.values()
        if isinstance(value, dict) and value.get("available") is True
    ]
    orphan_process_count = (
        sum(int(value) for value in orphan_values if isinstance(value, int))
        if len(orphan_values) == len(_OWNERSHIP_SCENARIOS)
        else None
    )
    freshness_checks_attempted = MUTATION_CYCLES * FRESHNESS_CHECKS_PER_CYCLE
    stale_result_rate = stale / freshness_checks_attempted
    orphan_process_rate = (
        orphan_process_count / len(_OWNERSHIP_SCENARIOS)
        if orphan_process_count is not None
        else None
    )

    references = precision_recall_f1(reference_expected, reference_actual)
    calls = precision_recall_f1(call_expected, call_actual)
    if mode == "qualification":
        performance = {
            "available": len(warm_facade) == PERFORMANCE_SAMPLES
            and all(
                value is not None
                for value in (
                    cold_readiness_seconds,
                    nearest_rank_percentile(warm_facade, 0.5),
                    nearest_rank_percentile(warm_facade, 0.95),
                    nearest_rank_percentile(direct_pyright, 0.95),
                    nearest_rank_percentile(overhead, 0.95),
                )
            ),
            "cold_readiness_seconds": cold_readiness_seconds,
            "warm_facade_p50_ms": nearest_rank_percentile(warm_facade, 0.5),
            "warm_facade_p95_ms": nearest_rank_percentile(warm_facade, 0.95),
            "direct_pyright_p95_ms": nearest_rank_percentile(direct_pyright, 0.95),
            "warm_overhead_p95_ms": nearest_rank_percentile(overhead, 0.95),
            "sample_count": len(warm_facade),
        }
    else:
        performance = {
            "available": False,
            "cold_readiness_seconds": None,
            "warm_facade_p50_ms": None,
            "warm_facade_p95_ms": None,
            "direct_pyright_p95_ms": None,
            "warm_overhead_p95_ms": None,
            "sample_count": 0,
        }
        resources = {
            "available": False,
            "client_peak_rss_mib": None,
            "method": "not_measured_in_correctness_only",
        }

    operator_metrics: dict[str, object] | None = None
    if operator_corpus is not None:
        probe = dependencies.operator_probe or _probe_operator_corpus
        operator_metrics = _operator_metrics(
            probe(
                operator_corpus,
                state_root,
                _operation_deadline(run_deadline, monotonic=monotonic),
            )
        )
        _check_run_deadline(run_deadline, monotonic=monotonic)

    if mode == "qualification":
        _check_run_deadline(run_deadline, monotonic=monotonic)
        peak_rss, rss_method = _peak_rss()
        _check_run_deadline(run_deadline, monotonic=monotonic)
        resources = {
            "available": peak_rss is not None,
            "client_peak_rss_mib": peak_rss,
            "method": rss_method,
        }

    _check_run_deadline(run_deadline, monotonic=monotonic)
    report: dict[str, object] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "mode": mode,
        "identity": {
            "source_manifest_sha256": repository.source_manifest_sha256,
            "gold_sha256": repository.gold_sha256,
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
            "git_commit_verified": scope.git_commit == commit,
            "identity_verified": True,
            "query_attempts": query_attempts,
            "mutation_cycles": mutation_cycles,
            "crash_attempts": crash_attempts,
            "ownership_checks": ownership_checks,
        },
        "correctness": {
            "definitions": {
                "attempted": definition_attempted,
                "exact": definition_exact,
                "accuracy": (
                    definition_exact / definition_attempted if definition_attempted else 0.0
                ),
            },
            "references": {"attempted": REFERENCE_QUERIES, **references},
            "calls": {"attempted": CALL_QUERIES, **calls},
            "task_success_rate": tasks_solved / len(repository.gold_queries),
            "citation_locations_attempted": citation_total,
            "citation_locations_correct": citation_correct,
            "citation_correctness_rate": (
                citation_correct / citation_total if citation_total else 0.0
            ),
        },
        "tokens": {
            "cache_read_label": "not_applicable_no_result_cache",
            "tasks": token_tasks,
            "default_items": default_items,
            "max_default_estimated_tokens": default_tokens,
        },
        "reliability": {
            "stale_answer_count": stale,
            "freshness_checks_attempted": freshness_checks_attempted,
            "freshness_checks_measured": freshness_checks_measured,
            "stale_result_rate": stale_result_rate,
            "mutation_cycles_measured": mutation_cycles,
            "edit_to_fresh_p50_ms": nearest_rank_percentile(freshness_latencies, 0.5),
            "edit_to_fresh_p95_ms": nearest_rank_percentile(freshness_latencies, 0.95),
            "crash_recoveries": crash_recoveries,
            "crash_attempts": crash_attempts,
            "recovery_rate": (crash_recoveries / crash_attempts if crash_attempts else 0.0),
            "orphan_process_count": orphan_process_count,
            "orphan_checks_attempted": len(_OWNERSHIP_SCENARIOS),
            "orphan_checks_measured": ownership_checks,
            "orphan_process_rate": orphan_process_rate,
            "ownership": ownership,
        },
        "performance": performance,
        "resources": resources,
        "operator_corpus": operator_metrics,
        "errors": errors,
        "market_superiority_claimed": False,
    }
    _check_run_deadline(run_deadline, monotonic=monotonic)
    validate_report(report)
    _check_run_deadline(run_deadline, monotonic=monotonic)
    return report


def _ratio_equal(actual: object, expected: float) -> bool:
    return (
        _finite_number(actual)
        and math.isfinite(expected)
        and math.isclose(
            float(actual),
            expected,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
    )


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _set_metric_consistent(metric: Mapping[str, object]) -> bool:
    true_positive = metric["true_positive"]
    false_positive = metric["false_positive"]
    false_negative = metric["false_negative"]
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (true_positive, false_positive, false_negative)
    ):
        return False
    actual_count = true_positive + false_positive
    expected_count = true_positive + false_negative
    precision = true_positive / actual_count if actual_count else 1.0
    recall = true_positive / expected_count if expected_count else 1.0
    f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
    return (
        _ratio_equal(metric["precision"], precision)
        and _ratio_equal(metric["recall"], recall)
        and _ratio_equal(metric["f1"], f1)
    )


def _evidence_complete(report: Mapping[str, object]) -> bool:
    try:
        identity = report["identity"]
        environment = report["environment"]
        evidence = report["evidence"]
        correctness = report["correctness"]
        reliability = report["reliability"]
        tokens = report["tokens"]
        ownership = reliability["ownership"]
        manifest = load_manifest()
        definitions = correctness["definitions"]
        references = correctness["references"]
        calls = correctness["calls"]
        token_tasks = tokens["tasks"]
        token_query_ids = [task["query_id"] for task in token_tasks]
        required_citation_locations = sum(
            5 if query_id.startswith("references-") else 1 for query_id in token_query_ids
        )
        valid_query_ids = {
            *(f"definition-{index:03d}" for index in range(DEFINITION_QUERIES)),
            *(f"references-{index:03d}" for index in range(REFERENCE_QUERIES)),
            *(f"calls-{index:03d}" for index in range(CALL_QUERIES)),
        }
        ownership_total = sum(
            ownership[scenario]["orphan_count"] for scenario in _OWNERSHIP_SCENARIOS
        )
        complete = (
            identity["source_manifest_sha256"] == manifest["expected_source_manifest_sha256"]
            and identity["gold_sha256"] == manifest["expected_gold_sha256"]
            and evidence["measured"] is True
            and evidence["source_hash_verified"] is True
            and evidence["gold_hash_verified"] is True
            and evidence["git_commit_verified"] is True
            and evidence["identity_verified"] is True
            and evidence["query_attempts"] == 400
            and evidence["mutation_cycles"] == 50
            and evidence["crash_attempts"] == 20
            and evidence["ownership_checks"] == 4
            and correctness["definitions"]["attempted"] == 200
            and references["attempted"] == 100
            and calls["attempted"] == 100
            and definitions["exact"] <= definitions["attempted"]
            and _ratio_equal(
                definitions["accuracy"],
                definitions["exact"] / definitions["attempted"],
            )
            and _set_metric_consistent(references)
            and references["true_positive"] + references["false_negative"] == 500
            and _set_metric_consistent(calls)
            and calls["true_positive"] + calls["false_negative"] == 100
            and _ratio_equal(
                correctness["task_success_rate"],
                len(token_tasks) / 400,
            )
            and correctness["citation_locations_attempted"] > 0
            and correctness["citation_locations_correct"]
            <= correctness["citation_locations_attempted"]
            and correctness["citation_locations_attempted"] >= required_citation_locations
            and correctness["citation_locations_correct"] >= required_citation_locations
            and _ratio_equal(
                correctness["citation_correctness_rate"],
                correctness["citation_locations_correct"]
                / correctness["citation_locations_attempted"],
            )
            and len(token_query_ids) == len(set(token_query_ids))
            and set(token_query_ids) <= valid_query_ids
            and reliability["freshness_checks_attempted"] == 250
            and reliability["freshness_checks_measured"] == 250
            and reliability["stale_answer_count"] <= 250
            and _ratio_equal(
                reliability["stale_result_rate"],
                reliability["stale_answer_count"] / 250,
            )
            and reliability["mutation_cycles_measured"] == 50
            and _finite_number(reliability["edit_to_fresh_p50_ms"])
            and reliability["edit_to_fresh_p50_ms"] >= 0
            and _finite_number(reliability["edit_to_fresh_p95_ms"])
            and reliability["edit_to_fresh_p95_ms"] >= 0
            and reliability["edit_to_fresh_p50_ms"] <= reliability["edit_to_fresh_p95_ms"]
            and reliability["crash_attempts"] == 20
            and reliability["crash_recoveries"] <= reliability["crash_attempts"]
            and _ratio_equal(
                reliability["recovery_rate"],
                reliability["crash_recoveries"] / reliability["crash_attempts"],
            )
            and reliability["orphan_checks_attempted"] == 4
            and reliability["orphan_checks_measured"] == 4
            and all(
                ownership[scenario]["available"] is True
                and ownership[scenario]["orphan_count"] is not None
                for scenario in _OWNERSHIP_SCENARIOS
            )
            and reliability["orphan_process_count"] == ownership_total
            and _ratio_equal(
                reliability["orphan_process_rate"],
                ownership_total / 4,
            )
            and tokens["cache_read_label"] == "not_applicable_no_result_cache"
            and all(task["cache_read_tokens"] == 0 for task in token_tasks)
            and report["errors"] == []
        )
        operator_corpus = report["operator_corpus"]
        if operator_corpus is not None:
            complete = (
                complete and operator_corpus["available"] is True and operator_corpus["errors"] == 0
            )
        if report["mode"] == "qualification":
            performance = report["performance"]
            resources = report["resources"]
            complete = (
                complete
                and environment["os"] == "Linux"
                and isinstance(identity["python_version"], str)
                and _PYTHON_310_VERSION.fullmatch(identity["python_version"]) is not None
                and performance["available"] is True
                and performance["sample_count"] == PERFORMANCE_SAMPLES
                and all(
                    _finite_number(performance[field])
                    for field in (
                        "cold_readiness_seconds",
                        "warm_facade_p50_ms",
                        "warm_facade_p95_ms",
                        "direct_pyright_p95_ms",
                        "warm_overhead_p95_ms",
                    )
                )
                and performance["warm_facade_p50_ms"] <= performance["warm_facade_p95_ms"]
                and resources["available"] is True
                and _finite_number(resources["client_peak_rss_mib"])
                and resources["method"] != "unavailable"
            )
        return bool(complete)
    except (ArithmeticError, KeyError, TypeError, ValueError):
        return False


def _gate_values(report: Mapping[str, object]) -> dict[str, object]:
    correctness = report["correctness"]
    reliability = report["reliability"]
    tokens = report["tokens"]
    performance = report["performance"]
    resources = report["resources"]
    return {
        "definition_accuracy": correctness["definitions"]["accuracy"],
        "reference_f1": correctness["references"]["f1"],
        "stale_answer_count": reliability["stale_answer_count"],
        "stale_result_rate": reliability["stale_result_rate"],
        "orphan_process_count": reliability["orphan_process_count"],
        "orphan_process_rate": reliability["orphan_process_rate"],
        "recovery_rate": reliability["recovery_rate"],
        "default_items": tokens["default_items"],
        "default_estimated_tokens": tokens["max_default_estimated_tokens"],
        "warm_overhead_p95_ms": performance["warm_overhead_p95_ms"],
        "cold_readiness_seconds": performance["cold_readiness_seconds"],
        "client_rss_mib": resources["client_peak_rss_mib"],
    }


def evaluate_gates(report: object) -> dict[str, object]:
    """Fail closed unless a schema-valid measured report has complete evidence."""
    try:
        validate_report(report)
    except (TypeError, ValueError):
        return {
            "passed": False,
            "schema_valid": False,
            "evidence_complete": False,
            "scope": "invalid",
            "gates": {},
        }
    assert isinstance(report, Mapping)
    complete = _evidence_complete(report)
    mode = report["mode"]
    scope = "qualification" if mode == "qualification" else "correctness_reliability"
    fields = _QUALIFICATION_GATES if mode == "qualification" else _CORRECTNESS_GATES
    values = _gate_values(report)
    minimums = {"definition_accuracy", "reference_f1", "recovery_rate"}
    gates: dict[str, dict[str, object]] = {}
    for field in fields:
        value = values[field]
        threshold = GATE_THRESHOLDS[field]
        measured = complete and value is not None
        if not measured or isinstance(value, bool) or not isinstance(value, (int, float)):
            passed = False
        elif field in minimums:
            passed = float(value) >= float(threshold)
        elif field == "client_rss_mib":
            passed = float(value) < float(threshold)
        else:
            passed = float(value) <= float(threshold)
        gates[field] = {
            "measured": measured,
            "value": value,
            "threshold": threshold,
            "passed": passed,
        }
    return {
        "passed": complete and all(item["passed"] for item in gates.values()),
        "schema_valid": True,
        "evidence_complete": complete,
        "scope": scope,
        "gates": gates,
    }


def resolve_state_root(
    argument: Path | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    environment = os.environ if environ is None else environ
    value: Path
    if argument is not None:
        value = Path(argument)
    elif environment.get("LLM_WIKI_STATE_ROOT"):
        value = Path(environment["LLM_WIKI_STATE_ROOT"])
    else:
        value = ROOT
    return value.expanduser().resolve()


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
    if args.operator_corpus is not None:
        operator_corpus = args.operator_corpus.expanduser()
        if not operator_corpus.is_absolute():
            parser.error("--operator-corpus must be absolute")
        operator_corpus = Path(os.path.abspath(os.fspath(operator_corpus)))
        try:
            metadata = operator_corpus.stat(follow_symlinks=False)
        except OSError:
            parser.error("--operator-corpus must exist")
        if _operator_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            parser.error("--operator-corpus must be a directory")
        args.operator_corpus = operator_corpus
    if args.state_root is not None and not args.state_root.is_absolute():
        parser.error("--state-root must be absolute")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    mode = "qualification" if args.qualification else "correctness-only"
    state_root = resolve_state_root(args.state_root)
    try:
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
    except BenchmarkTimeoutError:
        print(
            _canonical_json(
                {
                    "status": "error",
                    "code": "BenchmarkTimeout",
                    "market_superiority_claimed": False,
                }
            ),
            file=sys.stderr,
        )
        return 4
    except (FixtureIdentityError, QualifiedIdentityError) as exc:
        print(
            _canonical_json(
                {
                    "status": "error",
                    "code": type(exc).__name__,
                    "market_superiority_claimed": False,
                }
            ),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            _canonical_json(
                {
                    "status": "error",
                    "code": type(exc).__name__,
                    "market_superiority_claimed": False,
                }
            ),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
