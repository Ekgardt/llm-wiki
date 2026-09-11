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
_OWNERSHIP_PROBE_METHOD = "workspace/symbol"
# A cancellation probe only measures something when the request is still in
# flight. A fast machine can answer first, so give the probe a few tries.
_OWNERSHIP_PROBE_ATTEMPTS = 5
_OWNERSHIP_RESET_SECONDS = 60.0
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
    # 30 ms, not 20: three consecutive GitHub-hosted four-vCPU runs measured
    # 22.8, 22.08, and 22.16 ms. The facade walks the workspace revision an
    # extra time to guarantee freshness, and that cost is real on a slow
    # machine. Approved by the operator 2026-08-19; see
    # knowledge/notes/warm-navigation-overhead-threshold-decision.md.
    "warm_overhead_p95_ms": 30,
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


class _ProbeRacedError(RuntimeError):
    """Raised when the server answered before the probe could interrupt it."""


def _wait_one_poll(completed: threading.Event, deadline: float) -> None:
    """Yield for one poll interval, whether or not the request has finished."""
    wait_for = min(_OWNERSHIP_POLL_SECONDS, max(0.0, deadline - time.monotonic()))
    if completed.is_set():
        time.sleep(wait_for)
        return
    completed.wait(wait_for)


def _check_probe_error(error: BaseException, scenario: str) -> None:
    """The probe has to end in the terminal its scenario asked for."""
    expected = TimeoutError if scenario == "timeout" else RequestCancelled
    if isinstance(error, expected):
        return
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        raise error
    raise RuntimeError("ownership probe reached the wrong terminal") from error


def _check_probe_terminal(
    outcome: list[tuple[str, object]], scenario: str, dispatched: bool
) -> None:
    """What the probe ended with, refusing anything that measured nothing."""
    if not dispatched:
        raise RuntimeError("ownership probe request was not sent")
    if len(outcome) != 1:
        raise RuntimeError("ownership probe did not reach the expected terminal")
    if outcome[0][0] != "error":
        # The server answered inside the window between dispatch and the
        # interruption. Nothing was in flight to interrupt, so this attempt
        # measured nothing; the caller retries rather than calling the
        # scenario unavailable.
        raise _ProbeRacedError("ownership probe was answered before interruption")
    _check_probe_error(outcome[0][1], scenario)


def _probe_request_deadline(scenario: str, now: float, deadline: float) -> float:
    """The timeout scenario gets a short budget; the others get the full one."""
    if scenario != "timeout":
        return deadline
    return min(deadline, now + _OWNERSHIP_TIMEOUT_SECONDS)


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


_SCHEMA_TYPE_CHECKS: dict[str, Callable[[object], bool]] = {
    "null": lambda value: value is None,
    "boolean": lambda value: isinstance(value, bool),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: _finite_number(value),
    "string": lambda value: isinstance(value, str),
    "array": lambda value: isinstance(value, list),
    "object": lambda value: isinstance(value, dict),
}


def _schema_type_matches(value: object, expected: str) -> bool:
    check = _SCHEMA_TYPE_CHECKS.get(expected)
    if check is None:
        raise _SchemaViolation("unsupported schema type")
    return check(value)


def _schema_accepts(value: object, schema: object, root: Mapping[str, object]) -> bool:
    try:
        _validate_schema_node(value, schema, root)
    except _SchemaViolation:
        return False
    return True


def _reference_step(target: object, component: str) -> object:
    if not isinstance(target, dict) or component not in target:
        raise _SchemaViolation("schema reference is unresolved")
    return target[component]


def _resolve_reference(reference: object, root: Mapping[str, object]) -> object:
    if not isinstance(reference, str) or not reference.startswith("#/"):
        raise _SchemaViolation("only local schema references are supported")
    target: object = root
    for component in reference[2:].split("/"):
        target = _reference_step(target, component)
    return target


def _all_strings(values: object) -> bool:
    return isinstance(values, list) and all(isinstance(item, str) for item in values)


def _expected_types(expected_type: object) -> list[str]:
    expected_types = [expected_type] if isinstance(expected_type, str) else expected_type
    if not _all_strings(expected_types) or not expected_types:
        raise _SchemaViolation("schema type is invalid")
    return expected_types


def _validate_type(value: object, schema: dict) -> None:
    expected_type = schema.get("type")
    if expected_type is None:
        return
    if not any(_schema_type_matches(value, item) for item in _expected_types(expected_type)):
        raise _SchemaViolation("value has the wrong type")


def _in_enum(value: object, choices: object) -> bool:
    return isinstance(choices, list) and any(_json_equal(value, item) for item in choices)


def _validate_const_enum(value: object, schema: dict) -> None:
    if "const" in schema and not _json_equal(value, schema["const"]):
        raise _SchemaViolation("value differs from schema const")
    if "enum" in schema and not _in_enum(value, schema["enum"]):
        raise _SchemaViolation("value is outside schema enum")


def _require_required_keys(value: dict, required: list) -> None:
    if any(key not in value for key in required):
        raise _SchemaViolation("required property is absent")


def _require_no_additional(value: dict, schema: dict, properties: dict) -> None:
    if schema.get("additionalProperties") is False and any(key not in properties for key in value):
        raise _SchemaViolation("additional property is forbidden")


def _validate_properties(value: dict, properties: dict, root: Mapping[str, object]) -> None:
    for key, child_schema in properties.items():
        if key in value:
            _validate_schema_node(value[key], child_schema, root)


def _validate_object(value: dict, schema: dict, root: Mapping[str, object]) -> None:
    required = schema.get("required", [])
    properties = schema.get("properties", {})
    if not _all_strings(required):
        raise _SchemaViolation("schema required list is invalid")
    if not isinstance(properties, dict):
        raise _SchemaViolation("schema properties are invalid")
    _require_required_keys(value, required)
    _require_no_additional(value, schema, properties)
    _validate_properties(value, properties, root)


def _require_array_length(value: list, schema: dict) -> None:
    minimum_items = schema.get("minItems")
    maximum_items = schema.get("maxItems")
    if isinstance(minimum_items, int) and len(value) < minimum_items:
        raise _SchemaViolation("array is too short")
    if isinstance(maximum_items, int) and len(value) > maximum_items:
        raise _SchemaViolation("array is too long")


def _items_unique(value: list) -> bool:
    encoded = [_canonical_json(item) for item in value]
    return len(encoded) == len(set(encoded))


def _validate_array(value: list, schema: dict, root: Mapping[str, object]) -> None:
    _require_array_length(value, schema)
    if schema.get("uniqueItems") is True and not _items_unique(value):
        raise _SchemaViolation("array items are not unique")
    if "items" in schema:
        for item in value:
            _validate_schema_node(item, schema["items"], root)


def _matches_pattern(value: str, pattern: object) -> bool:
    if pattern is None:
        return True
    return isinstance(pattern, str) and re.search(pattern, value) is not None


def _validate_string(value: str, schema: dict) -> None:
    minimum_length = schema.get("minLength")
    if isinstance(minimum_length, int) and len(value) < minimum_length:
        raise _SchemaViolation("string is too short")
    if not _matches_pattern(value, schema.get("pattern")):
        raise _SchemaViolation("string does not match schema pattern")


def _validate_number(value: object, schema: dict) -> None:
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if _finite_number(minimum) and float(value) < float(minimum):
        raise _SchemaViolation("number is below schema minimum")
    if _finite_number(maximum) and float(value) > float(maximum):
        raise _SchemaViolation("number is above schema maximum")


def _validate_by_kind(value: object, schema: dict, root: Mapping[str, object]) -> None:
    if isinstance(value, dict):
        _validate_object(value, schema, root)
    if isinstance(value, list):
        _validate_array(value, schema, root)
    if isinstance(value, str):
        _validate_string(value, schema)
    if _finite_number(value):
        _validate_number(value, schema)


def _validate_one_of(value: object, schema: dict, root: Mapping[str, object]) -> None:
    one_of = schema.get("oneOf")
    if one_of is None:
        return
    if not isinstance(one_of, list) or sum(_schema_accepts(value, candidate, root) for candidate in one_of) != 1:
        raise _SchemaViolation("value does not match exactly one schema branch")


def _validate_branch(value: object, branch: object, root: Mapping[str, object]) -> None:
    if not isinstance(branch, dict):
        raise _SchemaViolation("schema allOf branch is invalid")
    condition = branch.get("if")
    if condition is None:
        _validate_schema_node(value, branch, root)
        return
    selected = branch.get("then", {}) if _schema_accepts(value, condition, root) else branch.get("else", {})
    _validate_schema_node(value, selected, root)


def _validate_all_of(value: object, schema: dict, root: Mapping[str, object]) -> None:
    all_of = schema.get("allOf", [])
    if not isinstance(all_of, list):
        raise _SchemaViolation("schema allOf is invalid")
    for branch in all_of:
        _validate_branch(value, branch, root)


def _validate_schema_node(
    value: object,
    schema: object,
    root: Mapping[str, object],
) -> None:
    if not isinstance(schema, dict):
        raise _SchemaViolation("schema node must be an object")
    reference = schema.get("$ref")
    if reference is not None:
        _validate_schema_node(value, _resolve_reference(reference, root), root)
        return
    _validate_type(value, schema)
    _validate_const_enum(value, schema)
    _validate_by_kind(value, schema, root)
    _validate_one_of(value, schema, root)
    _validate_all_of(value, schema, root)


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


def _ordered_finite_samples(values: Sequence[float]) -> list[float]:
    ordered = sorted(float(value) for value in values)
    if not all(math.isfinite(value) for value in ordered):
        raise ValueError("percentile samples must be finite")
    return ordered


def nearest_rank_percentile(values: Sequence[float], percentile: float) -> float | None:
    """Return the nearest-rank percentile: sorted[ceil(p*n)-1]."""
    if not 0.0 < percentile <= 1.0 or not math.isfinite(percentile):
        raise ValueError("percentile must be finite and in (0, 1]")
    if not values:
        return None
    ordered = _ordered_finite_samples(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _queries_by_capability(queries: Sequence[GoldQuery]) -> dict[str, tuple[GoldQuery, ...]]:
    return {
        capability: tuple(query for query in queries if query.capability == capability)
        for capability in ("definition", "references", "calls")
    }


def _require_complete_domain(by_capability: dict[str, tuple[GoldQuery, ...]]) -> None:
    expected_counts = {
        "definition": DEFINITION_QUERIES,
        "references": REFERENCE_QUERIES,
        "calls": CALL_QUERIES,
    }
    if any(len(by_capability[capability]) != expected for capability, expected in expected_counts.items()):
        raise ValueError("performance sample requires the complete gold query domain")


def _spread(candidates: tuple[GoldQuery, ...], count: int) -> tuple[GoldQuery, ...]:
    denominator = count - 1
    return tuple(
        candidates[(index * (len(candidates) - 1) + denominator // 2) // denominator]
        for index in range(count)
    )


def _interleaved(definitions, references, calls) -> tuple[GoldQuery, ...]:
    return tuple(
        query
        for index in range(5)
        for query in (definitions[index * 2], references[index], calls[index], definitions[index * 2 + 1])
    )


def _performance_queries(queries: Sequence[GoldQuery]) -> tuple[GoldQuery, ...]:
    by_capability = _queries_by_capability(queries)
    _require_complete_domain(by_capability)
    selected = _interleaved(
        _spread(by_capability["definition"], 10),
        _spread(by_capability["references"], 5),
        _spread(by_capability["calls"], 5),
    )
    if len(selected) != PERFORMANCE_SAMPLES or len({query.query_id for query in selected}) != len(selected):
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


def _git_timeout(deadline: float | None, monotonic: Callable[[], float]) -> float:
    if deadline is None:
        return 30.0
    operation_end = _operation_deadline(deadline, monotonic=monotonic)
    return min(30.0, max(0.001, operation_end - monotonic()))


def _git_timeout_error(deadline: float | None, monotonic: Callable[[], float]) -> Exception:
    if deadline is not None and monotonic() >= deadline:
        return BenchmarkTimeoutError("benchmark run deadline exceeded")
    return RuntimeError("deterministic qualification Git command timed out")


def _run_git(
    command: list[str],
    root: Path,
    environment: dict[str, str],
    timeout: float,
    capture: bool,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            command,
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
        raise _git_timeout_error(deadline, monotonic) from exc
    if result.returncode != 0:
        raise RuntimeError("deterministic qualification Git command failed")
    if deadline is not None:
        _check_run_deadline(deadline, monotonic=monotonic)
    return result


def _git_stdout(result: subprocess.CompletedProcess, capture: bool) -> str:
    if not capture:
        return ""
    return result.stdout.decode("ascii", errors="strict").strip()


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
            timeout = _git_timeout(deadline, monotonic)
            result = _run_git(
                [*command_prefix, *arguments], root, environment, timeout, capture, deadline, monotonic
            )
            return _git_stdout(result, capture)

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


def _require_source_hash(repository: QualificationRepository, manifest: Mapping[str, object]) -> None:
    current = current_source_manifest_sha256(repository.root)
    if current != repository.source_manifest_sha256 or current != manifest.get("expected_source_manifest_sha256"):
        raise FixtureIdentityError("generated source manifest hash does not match manifest")


def _require_gold_hash(repository: QualificationRepository, manifest: Mapping[str, object]) -> None:
    current = current_gold_sha256(repository)
    if current != repository.gold_sha256 or current != manifest.get("expected_gold_sha256"):
        raise FixtureIdentityError("generated gold hash does not match manifest")


def verify_repository_identity(
    repository: QualificationRepository,
    manifest: Mapping[str, object],
) -> None:
    if repository.line_count != manifest.get("fixture_lines"):
        raise FixtureIdentityError("generated fixture line count does not match manifest")
    _require_source_hash(repository, manifest)
    catalog = (repository.root / WORKLOAD_CATALOG_PATH).read_bytes()
    if catalog != workload_catalog_bytes(repository.workloads):
        raise FixtureIdentityError("generated workload catalog does not match workloads")
    _require_gold_hash(repository, manifest)


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


def _expected_hashes(query: GoldQuery) -> dict[tuple[object, ...], str]:
    return {_expected_key(location): location.source_sha256 for location in query.expected_locations}


def _direct_location_key(
    scope: RepositoryScope, location: object, encoding: PositionEncoding, deadline: float
) -> tuple[tuple[object, ...], str] | None:
    """(key, source sha256) for one provider location; None when it cannot be resolved."""
    if time.monotonic() >= deadline or not isinstance(location, LspLocation):
        return None
    source = normalize_provider_uri(scope, location.uri)
    if source is None:
        return None
    try:
        content = read_repository_source_bytes(
            scope,
            source.relative_path,
            max_bytes=_DIRECT_VALIDATION_MAX_SOURCE_BYTES,
            deadline=deadline,
        )
        document = SourceDocument.from_bytes(source.relative_path, content)
        range_ = document.to_byte_range(location.range, encoding)
        line_start, _line_end = document.line_spans[location.range.start.line]
    except Exception:
        return None
    key = (
        source.relative_path,
        location.range.start.line + 1,
        range_.byte_start - line_start,
        range_.byte_start,
        range_.byte_end,
    )
    return key, document.source_sha256


def _resolved_matches(resolved: tuple[tuple[object, ...], str] | None, expected_hashes: dict) -> bool:
    return resolved is not None and resolved[1] == expected_hashes.get(resolved[0])


def _direct_result_keys(
    scope: RepositoryScope, result: object, encoding: PositionEncoding, expected_hashes: dict, deadline: float
) -> set[tuple[object, ...]] | None:
    if getattr(result, "coverage", None) != "provider_reported":
        return None
    locations = getattr(result, "locations", None)
    if not isinstance(locations, tuple):
        return None
    actual: set[tuple[object, ...]] = set()
    for location in locations:
        resolved = _direct_location_key(scope, location, encoding, deadline)
        if not _resolved_matches(resolved, expected_hashes):
            return None
        actual.add(resolved[0])
    return actual


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
    expected_hashes = _expected_hashes(query)
    for result in results:
        if _direct_result_keys(scope, result, encoding, expected_hashes, deadline) != expected:
            return False
    return True


def _cited_content(repository_root: Path, relative: str) -> bytes:
    root = repository_root.resolve(strict=True)
    path = (root / relative).resolve(strict=True)
    path.relative_to(root)
    return path.read_bytes()


def _citation_span_current(content: bytes, location: NavigationLocation) -> bool:
    start = location.range.byte_start
    end = location.range.byte_end
    if not 0 <= start < end <= len(content):
        return False
    content[:start].decode("utf-8", errors="strict")
    content[start:end].decode("utf-8", errors="strict")
    line_start = content.rfind(b"\n", 0, start) + 1
    return content.count(b"\n", 0, start) + 1 == location.line and start - line_start == location.character


def _current_citation(
    repository_root: Path,
    location: NavigationLocation,
    *,
    expected_sha256: str,
) -> bool:
    try:
        if _HEX_SHA256.fullmatch(expected_sha256) is None:
            return False
        content = _cited_content(repository_root, location.path)
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            return False
        return _citation_span_current(content, location)
    except (OSError, UnicodeError, ValueError):
        return False


def _group_location_count(group: object) -> int:
    if isinstance(group, dict) and isinstance(group.get("locations"), list):
        return len(group["locations"])
    return 0


def _rendered_item_count(payload: Mapping[str, object]) -> int:
    groups = payload.get("groups", [])
    diagnostics = payload.get("diagnostics", [])
    locations = sum(_group_location_count(group) for group in groups) if isinstance(groups, list) else 0
    return locations + (len(diagnostics) if isinstance(diagnostics, list) else 0)


def _request_value(request: NavigationRequest) -> dict[str, object]:
    return {
        "capability": request.capability.value,
        "path": request.path,
        "line": request.line,
        "character": request.character,
        "direction": request.direction,
        "offset": request.offset,
        "limit": request.limit,
    }


def _provenance_value(item: object) -> dict[str, str]:
    return {
        "source": item.source,
        "provider": item.provider,
        "version": item.version,
        "observation": item.observation,
    }


def _location_value(location: NavigationLocation) -> dict[str, object]:
    return {
        "path": location.path,
        "range": {"byte_start": location.range.byte_start, "byte_end": location.range.byte_end},
        "line": location.line,
        "character": location.character,
        "containing_symbol": location.containing_symbol,
        "signature": location.signature,
        "resolution": location.resolution.value,
        "provenance": [_provenance_value(item) for item in location.provenance],
    }


def _diagnostic_value(diagnostic: object) -> dict[str, object]:
    return {
        "path": diagnostic.path,
        "range": {"byte_start": diagnostic.range.byte_start, "byte_end": diagnostic.range.byte_end},
        "severity": diagnostic.severity.value,
        "code": diagnostic.code,
        "message": diagnostic.message,
        "related": [_location_value(location) for location in diagnostic.related],
        "provenance": [_provenance_value(item) for item in diagnostic.provenance],
    }


def _enum_value_or_none(value: object) -> object:
    if value is None:
        return None
    return value.value


def _raw_result_value(result: NavigationResult) -> dict[str, object]:
    return {
        "status": result.status.value,
        "requested_capability": result.requested_capability.value,
        "effective_capability": _enum_value_or_none(result.effective_capability),
        "provider": result.provider,
        "provider_version": result.provider_version,
        "repository_id": result.repository_id,
        "checkout_id": result.checkout_id,
        "workspace_revision_before": result.workspace_revision_before,
        "workspace_revision_after": result.workspace_revision_after,
        "document_version": result.document_version,
        "position_encoding": _enum_value_or_none(result.position_encoding),
        "readiness": result.readiness,
        "symbol": result.symbol,
        "total": result.total,
        "offset": result.offset,
        "limit": result.limit,
        "locations": [_location_value(location) for location in result.locations],
        "diagnostics": [_diagnostic_value(diagnostic) for diagnostic in result.diagnostics],
        "hover": result.hover,
        "resolution": result.resolution.value,
        "provenance": [_provenance_value(item) for item in result.provenance],
        "warnings": list(result.warnings),
    }


def _token_record(
    query: GoldQuery,
    request: NavigationRequest,
    result: NavigationResult,
    rendered: Mapping[str, object],
) -> dict[str, object]:
    return {
        "query_id": query.query_id,
        "uncached_input_tokens": estimate_tokens(_canonical_json(_request_value(request))),
        "cache_read_tokens": 0,
        "raw_tool_tokens": estimate_tokens(_canonical_json(_raw_result_value(result))),
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


def _citations_current(repository_root: Path, locations, expected_hashes: dict) -> bool:
    return all(
        _current_citation(repository_root, location, expected_sha256=expected_hashes.get(_actual_key(location), ""))
        for location in locations
    )


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


def _process_alive(popen: object) -> bool:
    return popen is not None and popen.poll() is None


def _coordinator_owns(coordinator: object) -> bool:
    return coordinator is not None and _coordinator_has_ownership(coordinator)


def _lease_exists(lease: Path | None) -> bool:
    return lease is not None and lease.exists()


def _process_handles(process: object) -> tuple[object, object, Path | None]:
    """(popen, coordinator, lease path) of a live process; Nones when there is none."""
    if process is None:
        return None, None, None
    return process.process, process._coordinator, process.owner_root / "lease.json"


def _orphan_evidence_problem(orphan: object) -> str | None:
    if isinstance(orphan, bool) or orphan not in {0, 1}:
        return "runtime cleanup returned invalid evidence"
    if orphan:
        return "runtime cleanup reported retained ownership"
    return None


def _retry_once_after_oserror(retried: bool, deadline: float) -> bool:
    """True once: a second OSError, or one past the deadline, propagates."""
    if retried or time.monotonic() >= deadline:
        raise
    return True


def _still_not_ready(recovered: object, deadline: float) -> bool:
    if not isinstance(recovered, NavigationResult):
        return False
    return recovered.status is NavigationStatus.NOT_READY and time.monotonic() < deadline


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
        popen, coordinator, lease = _process_handles(self._session._process)
        self._navigation.close(deadline=deadline)
        return int(_process_alive(popen) or _coordinator_owns(coordinator) or _lease_exists(lease))

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

    def _fail_cleanup(self) -> None:
        self._cleanup_failed = True
        self._retry_retained_cleanup()

    def _reset(self, deadline: float) -> int:
        if self.cleanup_failed:
            raise _CleanupProofError("runtime cleanup is terminal")
        try:
            orphan = self._close_and_count(deadline)
        except Exception as exc:
            self._fail_cleanup()
            raise _CleanupProofError("runtime cleanup could not be proven") from exc
        problem = _orphan_evidence_problem(orphan)
        if problem is not None:
            self._fail_cleanup()
            raise _CleanupProofError(problem)
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
                retried_oserror = _retry_once_after_oserror(retried_oserror, deadline)
                continue
            if not _still_not_ready(recovered, deadline):
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

    def _start_probe_request(
        self,
        process: object,
        scenario: str,
        request_deadline: float,
        source: CancellationSource,
        outcome: list[tuple[str, object]],
        completed: threading.Event,
    ) -> threading.Thread:
        """Send the probe request from a worker so the caller can interrupt it."""

        def request_workspace_symbols() -> None:
            try:
                result = process.request(
                    _OWNERSHIP_PROBE_METHOD,
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
        return worker

    def _await_probe_dispatch(
        self,
        protocol: object,
        baseline_sequence: int,
        completed: threading.Event,
        deadline: float,
    ) -> bool:
        """Whether the probe request reached the wire before the deadline."""
        while time.monotonic() < deadline:
            sent_sequence, sent_method = protocol._sent_request_evidence()
            if (
                sent_sequence > baseline_sequence
                and sent_method == _OWNERSHIP_PROBE_METHOD
            ):
                return True
            _wait_one_poll(completed, deadline)
        return False

    def _join_probe_worker(
        self, worker: threading.Thread, completed: threading.Event, deadline: float
    ) -> None:
        """The probe owns a thread and a request; neither may outlive the probe."""
        if not completed.wait(max(0.0, deadline - time.monotonic())):
            self._cleanup_failed = True
            raise _CleanupProofError("ownership probe request did not terminate")
        worker.join(max(0.0, deadline - time.monotonic()))
        if worker.is_alive():
            self._cleanup_failed = True
            raise _CleanupProofError("ownership probe request owner did not stop")

    def _observe_inflight_interruption(
        self,
        process: object,
        scenario: str,
        deadline: float,
    ) -> None:
        protocol = process.protocol
        baseline_sequence, _baseline_method = protocol._sent_request_evidence()
        source = CancellationSource()
        now = time.monotonic()
        request_deadline = _probe_request_deadline(scenario, now, deadline)
        if now >= request_deadline:
            raise TimeoutError("ownership probe deadline expired")

        completed = threading.Event()
        outcome: list[tuple[str, object]] = []
        worker = self._start_probe_request(
            process, scenario, request_deadline, source, outcome, completed
        )
        dispatched = self._await_probe_dispatch(
            protocol, baseline_sequence, completed, deadline
        )
        if dispatched and scenario == "cancellation":
            source.cancel()
        self._join_probe_worker(worker, completed, deadline)
        _check_probe_terminal(outcome, scenario, dispatched)

    def ownership_checks(self, *, deadline: float) -> dict[str, dict[str, int | bool | None]]:
        outcomes: dict[str, dict[str, int | bool | None]] = {
            scenario: {"available": False, "orphan_count": None}
            for scenario in _OWNERSHIP_SCENARIOS
        }
        for scenario in _OWNERSHIP_SCENARIOS:
            if self.cleanup_failed:
                break
            outcomes[scenario] = self._ownership_outcome(scenario, deadline)
        return outcomes

    def _can_attempt_ownership(self, deadline: float) -> bool:
        return not self.cleanup_failed and time.monotonic() < deadline

    def _attempt_ownership_scenario(
        self, scenario: str, deadline: float
    ) -> tuple[int | None, bool]:
        """The retained-owner count, or None and whether a retry is worthwhile."""
        try:
            return self._run_ownership_scenario(scenario, deadline), False
        except _ProbeRacedError:
            self._recover_ownership_scenario()
            return None, True
        except Exception:
            self._recover_ownership_scenario()
            return None, False

    def _ownership_outcome(
        self, scenario: str, deadline: float
    ) -> dict[str, int | bool | None]:
        """One scenario's result, retrying attempts that measured nothing."""
        for _attempt in range(_OWNERSHIP_PROBE_ATTEMPTS):
            if not self._can_attempt_ownership(deadline):
                break
            orphan, retry = self._attempt_ownership_scenario(scenario, deadline)
            if orphan is not None:
                return {"available": True, "orphan_count": orphan}
            if not retry:
                break
        return {"available": False, "orphan_count": None}

    def _recover_ownership_scenario(self) -> None:
        """Put the harness back where the next attempt can start."""
        if self.cleanup_failed:
            return
        with contextlib.suppress(Exception):
            self._reset(time.monotonic() + _OWNERSHIP_RESET_SECONDS)

    def _run_ownership_scenario(self, scenario: str, deadline: float) -> int:
        """Drive one scenario to its terminal and return the retained owners."""
        request, process = self._prepare_process(deadline)
        if scenario == "crash":
            process.process.kill()
            self._query_after_crash(request, deadline)
        elif scenario in {"timeout", "cancellation"}:
            self._observe_inflight_interruption(process, scenario, deadline)
        return self._reset(deadline)

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


def _windows_peak_rss() -> tuple[float, str]:
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
    return counters.PeakWorkingSetSize / (1024.0 * 1024.0), "measured-windows-peak-working-set"


def _posix_peak_rss() -> tuple[float, str]:
    import resource

    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    multiplier = 1 if sys.platform == "darwin" else 1024
    return usage * multiplier / (1024.0 * 1024.0), "measured-posix-ru-maxrss"


def _peak_rss() -> tuple[float | None, str]:
    if os.name == "nt":
        probe, failures = _windows_peak_rss, (AttributeError, OSError, TypeError, ValueError)
    else:
        probe, failures = _posix_peak_rss, (ImportError, OSError, ValueError)
    try:
        return probe()
    except failures:
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


def _first_available(*values: str) -> str:
    for value in values:
        if value:
            return value
    return "unavailable"


def _environment() -> dict[str, object]:
    return {
        "os": platform.system() or os.name,
        "os_version": _first_available(platform.version(), platform.release()),
        "architecture": _first_available(platform.machine()),
        "cpu_model": _first_available(platform.processor(), platform.machine()),
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


def _require_plain_directory(info: object) -> None:
    if _operator_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise PermissionError("operator source parent must be a regular directory")


def _validate_operator_source_chain(path: Path, root: Path, deadline: float) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PermissionError("operator source is outside operator root") from exc
    current = root
    while True:
        _check_run_deadline(deadline)
        _require_plain_directory(current.stat(follow_symlinks=False))
        if current == path.parent:
            return
        current /= path.parent.relative_to(current).parts[0]


def _expected_operator_state(source: Path) -> tuple[int, int, int, int, int, int]:
    before = source.stat(follow_symlinks=False)
    if _operator_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise PermissionError("operator source must be a regular file")
    if before.st_size > OPERATOR_MAX_SOURCE_BYTES:
        raise ValueError("operator source exceeds the byte limit")
    return _operator_file_state(before)


def _windows_source_state(handle: int) -> tuple[object, ...]:
    return (
        _windows_workspace.identity(handle, directory=False),
        _windows_workspace.file_size(handle),
        _windows_workspace.file_modified_time_ns(handle),
    )


def _open_operator_source(source: Path, flags: int) -> tuple[int, int | None, tuple[object, ...] | None]:
    """(descriptor, Windows handle, Windows state); the handle and state are None on POSIX."""
    if os.name != "nt":
        descriptor = os.open(source, flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        return descriptor, None, None
    import msvcrt

    handle = _windows_workspace.open_exclusive_readonly_source_file(source)
    try:
        state = _windows_source_state(handle)
        descriptor = msvcrt.open_osfhandle(handle, flags)
    except BaseException:
        _windows_workspace.close_handle(handle)
        raise
    return descriptor, handle, state


def _read_operator_chunks(descriptor: int, deadline: float) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= OPERATOR_MAX_SOURCE_BYTES:
        _check_run_deadline(deadline)
        chunk = os.read(descriptor, min(OPERATOR_READ_CHUNK_BYTES, OPERATOR_MAX_SOURCE_BYTES + 1 - total))
        _check_run_deadline(deadline)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > OPERATOR_MAX_SOURCE_BYTES:
        raise ValueError("operator source exceeds the byte limit")
    return b"".join(chunks)


def _require_source_unchanged(
    descriptor: int, expected_state: tuple, windows_handle: int | None, windows_state: tuple | None
) -> None:
    if _operator_file_state(os.fstat(descriptor)) != expected_state:
        raise PermissionError("operator source changed during read")
    if windows_handle is not None and _windows_source_state(windows_handle) != windows_state:
        raise PermissionError("operator source changed during read")


def _require_source_not_replaced(source: Path, expected_state: tuple) -> None:
    current = source.stat(follow_symlinks=False)
    if _operator_link_or_reparse(current) or _operator_file_state(current) != expected_state:
        raise PermissionError("operator source was replaced during read")


def _read_operator_source(path: Path, operator_root: Path, *, deadline: float) -> bytes:
    source = Path(os.path.abspath(os.fspath(path)))
    root = Path(os.path.abspath(os.fspath(operator_root)))
    _validate_operator_source_chain(source, root, deadline)
    _check_run_deadline(deadline)
    expected_state = _expected_operator_state(source)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    _check_run_deadline(deadline)
    descriptor, windows_handle, windows_state = _open_operator_source(source, flags)
    try:
        _check_run_deadline(deadline)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _operator_file_state(opened) != expected_state:
            raise PermissionError("operator source changed before open")
        content = _read_operator_chunks(descriptor, deadline)
        _require_source_unchanged(descriptor, expected_state, windows_handle, windows_state)
        _check_run_deadline(deadline)
        _require_source_not_replaced(source, expected_state)
        _validate_operator_source_chain(source, root, deadline)
        return content
    finally:
        os.close(descriptor)


def _definition_token_action(token, expect_name: bool) -> str | None:
    """`start` at def/class, `name` at the name that follows, `reset` at anything else."""
    if _starts_definition(token):
        return "start"
    if not expect_name:
        return None
    if token.type == tokenize.NAME:
        return "name"
    if token.type not in {tokenize.NL, tokenize.INDENT}:
        return "reset"
    return None


def _starts_definition(token) -> bool:
    return token.type == tokenize.NAME and token.string in {"def", "class"}


def _definition_position(content: bytes, token, path: Path, root: Path) -> tuple[str, int, int]:
    line_bytes = content.splitlines()[token.start[0] - 1]
    prefix = line_bytes.decode("utf-8")[: token.start[1]].encode("utf-8")
    return path.relative_to(root).as_posix(), token.start[0], len(prefix)


def _operator_definition(
    path: Path,
    root: Path,
    *,
    operator_root: Path,
    deadline: float,
) -> tuple[str, int, int] | None:
    content = _read_operator_source(path, operator_root, deadline=deadline)
    expect_name = False
    for token in tokenize.tokenize(io.BytesIO(content).readline):
        action = _definition_token_action(token, expect_name)
        if action == "name":
            return _definition_position(content, token, path, root)
        if action is not None:
            expect_name = action == "start"
    return None


_REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _require_operator_root(requested: Path) -> Path:
    try:
        metadata = requested.stat(follow_symlinks=False)
    except OSError as exc:
        raise _OperatorTraversalError("operator root is unavailable") from exc
    if requested.is_symlink() or getattr(metadata, "st_file_attributes", 0) & _REPARSE_FLAG or not stat.S_ISDIR(metadata.st_mode):
        raise _OperatorTraversalError("operator root is not a regular directory")
    return requested.resolve(strict=True)


class _OperatorScan:
    """A bounded depth-first walk over an operator corpus."""

    def __init__(self, root: Path, deadline: float) -> None:
        self.root = root
        self.deadline = deadline
        self.files: list[Path] = []
        self.stack: list[tuple[Path, int]] = [(root, 0)]
        self.visited: set[tuple[int, int]] = set()
        self.scanned_entries = 0


def _directory_identity(current: Path) -> tuple[int, int]:
    try:
        metadata = current.stat(follow_symlinks=False)
    except OSError as exc:
        raise _OperatorTraversalError("operator directory is unavailable") from exc
    return metadata.st_dev, metadata.st_ino


def _scan_entries(scan: _OperatorScan, current: Path) -> list:
    try:
        with os.scandir(current) as iterator:
            entries = []
            for entry in iterator:
                _check_run_deadline(scan.deadline)
                scan.scanned_entries += 1
                if scan.scanned_entries > OPERATOR_MAX_SCANNED_ENTRIES:
                    raise _OperatorTraversalError("operator traversal exceeds the entry limit")
                entries.append(entry)
    except OSError as exc:
        raise _OperatorTraversalError("operator directory cannot be scanned") from exc
    return entries


def _entry_metadata(entry):
    try:
        return entry.stat(follow_symlinks=False)
    except OSError as exc:
        raise _OperatorTraversalError("operator entry cannot be inspected") from exc


def _entry_is_link(entry, metadata) -> bool:
    return entry.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_FLAG)


def _classify_entry(entry, metadata) -> str:
    if _entry_is_link(entry, metadata):
        return "skip"
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    if stat.S_ISREG(metadata.st_mode) and Path(entry.path).suffix == ".py":
        return "python"
    return "other"


def _child_directory(path: Path, depth: int) -> tuple[Path, int]:
    if depth + 1 > OPERATOR_MAX_DEPTH:
        raise _OperatorTraversalError("operator traversal exceeds the depth limit")
    return path, depth + 1


def _contained_python_file(path: Path, root: Path) -> Path:
    try:
        candidate = path.resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError) as exc:
        raise _OperatorTraversalError("operator file escaped its root") from exc
    return candidate


def _visit_directory(scan: _OperatorScan, current: Path, depth: int) -> None:
    entries = _scan_entries(scan, current)
    child_directories: list[tuple[Path, int]] = []
    for entry in sorted(entries, key=lambda item: item.name):
        _check_run_deadline(scan.deadline)
        kind = _classify_entry(entry, _entry_metadata(entry))
        path = Path(entry.path)
        if kind == "directory":
            child_directories.append(_child_directory(path, depth))
            continue
        if kind != "python":
            continue
        scan.files.append(_contained_python_file(path, scan.root))
        if len(scan.files) >= OPERATOR_MAX_PYTHON_FILES:
            break
    scan.stack.extend(reversed(child_directories))


def _operator_python_files(
    operator_root: Path,
    *,
    deadline: float,
) -> tuple[Path, list[Path]]:
    root = _require_operator_root(Path(operator_root))
    scan = _OperatorScan(root, deadline)
    while scan.stack and len(scan.files) < OPERATOR_MAX_PYTHON_FILES:
        _check_run_deadline(deadline)
        current, depth = scan.stack.pop()
        identity = _directory_identity(current)
        if identity in scan.visited:
            continue
        scan.visited.add(identity)
        _visit_directory(scan, current, depth)
    return root, scan.files


class _ProbeTally:
    def __init__(self) -> None:
        self.attempts = 0
        self.successes = 0
        self.errors = 0
        self.available = True

    def fail(self) -> None:
        self.errors += 1
        self.available = False


def _probe_one_file(
    navigation: CodeNavigation,
    scope: RepositoryScope,
    checkout: Path,
    root: Path,
    path: Path,
    tally: _ProbeTally,
    deadline: float,
) -> None:
    query_deadline = _operation_deadline(deadline)
    definition = _operator_definition(path, checkout, operator_root=root, deadline=query_deadline)
    _check_run_deadline(deadline)
    if definition is None:
        return
    relative, line, character = definition
    tally.attempts += 1
    result = navigation.query(
        NavigationRequest(scope, Capability.DEFINITIONS, relative, line, character),
        deadline=query_deadline,
    )
    if result.status in {NavigationStatus.OK, NavigationStatus.PARTIAL}:
        tally.successes += 1
    _check_run_deadline(deadline)


def _probe_files(
    navigation: CodeNavigation,
    scope: RepositoryScope,
    root: Path,
    files: list[Path],
    tally: _ProbeTally,
    deadline: float,
) -> None:
    checkout = Path(scope.checkout_root)
    for path in files:
        try:
            _probe_one_file(navigation, scope, checkout, root, path, tally, deadline)
        except BenchmarkTimeoutError:
            raise
        except Exception:
            tally.fail()
            return


def _open_operator_navigation(root: Path, state_root: Path, deadline: float) -> tuple[CodeNavigation, RepositoryScope]:
    _check_run_deadline(deadline)
    scope = resolve_repository_scope(root)
    identity = discover_pyright(scope, state_root=state_root, deadline=_operation_deadline(deadline))
    _check_run_deadline(deadline)
    _require_qualified_identity(identity, load_manifest())
    session = PyrightSession(scope, identity, state_root=state_root)
    return CodeNavigation(scope, session, identity), scope


def _close_operator_navigation(navigation: CodeNavigation | None, tally: _ProbeTally) -> None:
    if navigation is None:
        return
    try:
        navigation.close(deadline=_fresh_cleanup_deadline())
    except Exception:
        tally.fail()
        try:
            navigation.close(deadline=_fresh_cleanup_deadline())
        except Exception:
            tally.errors += 1


def _probe_operator_corpus(
    operator_root: Path,
    state_root: Path,
    deadline: float,
) -> dict[str, object]:
    files: list[Path] = []
    tally = _ProbeTally()
    navigation: CodeNavigation | None = None
    try:
        root, files = _operator_python_files(operator_root, deadline=deadline)
        navigation, scope = _open_operator_navigation(root, state_root, deadline)
        _probe_files(navigation, scope, root, files, tally, deadline)
    except BenchmarkTimeoutError:
        raise
    except Exception:
        tally.fail()
    finally:
        _close_operator_navigation(navigation, tally)
    return {
        "available": tally.available,
        "python_files": len(files),
        "queries_attempted": tally.attempts,
        "queries_succeeded": tally.successes,
        "errors": tally.errors,
    }


def _is_count(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value >= 0


def _counts_valid(value: dict, keys: set[str]) -> bool:
    return all(_is_count(value[key]) for key in keys - {"available"})


def _operator_metrics(value: object) -> dict[str, object]:
    keys = {"available", "python_files", "queries_attempted", "queries_succeeded", "errors"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("operator corpus probe returned a non-aggregate result")
    if not isinstance(value["available"], bool) or not _counts_valid(value, keys):
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
