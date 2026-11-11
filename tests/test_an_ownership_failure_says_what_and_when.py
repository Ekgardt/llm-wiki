"""An ownership attempt that measured nothing says which failure, what it said, and how long it ran.

Four places raised a bare RuntimeError, so the report named all of them
`RuntimeError`; a failed reset between attempts was swallowed; and nothing said
how coarse the clock was that read the 50 ms probe budget (audit 2026-09-27 B-8).
docs/research/2026-09-27-an-ownership-failure-says-what-and-when.md
"""
from __future__ import annotations

import ast
import sys
import time
from pathlib import Path

import pytest

BENCHMARK_ROOT = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK_ROOT) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_ROOT))

import navigation_benchmark_ownership as ownership  # noqa: E402
from navigation_benchmark_ownership import _RealNavigationRuntime  # noqa: E402
from run_code_navigation import _FixtureRun  # noqa: E402


def _code_of(action) -> str:
    with pytest.raises(ownership._OwnershipProbeError) as caught:
        action()
    return ownership._unmeasured_reason(caught.value)


def test_each_bare_failure_has_its_own_code() -> None:
    codes = [
        _code_of(lambda: ownership._require_single_terminal([], False)),
        _code_of(lambda: ownership._require_single_terminal([], True)),
        _code_of(lambda: ownership._check_probe_error(ValueError("late"), "timeout")),
    ]

    assert codes == ["not_sent", "no_single_terminal", "wrong_terminal:ValueError"]


def test_the_message_carries_the_cause_the_duration_and_the_clock() -> None:
    error = ownership._OwnershipProbeError("wrong_terminal", "ownership probe reached the wrong terminal")
    error.__cause__ = ValueError("answered")

    message = ownership._unmeasured_message(error, 0.0625)

    assert message.startswith("ownership probe reached the wrong terminal: answered (after 62.5 ms; monotonic resolution ")


def _runtime(error: BaseException, reset) -> _RealNavigationRuntime:
    runtime = object.__new__(_RealNavigationRuntime)
    runtime._cleanup_failed = False

    def run(scenario: str, deadline: float) -> int:
        raise error

    runtime._run_ownership_scenario = run
    runtime._reset = reset
    return runtime


def _failing_reset(deadline: float) -> int:
    raise OSError("the server would not stop")


def test_a_failed_recovery_is_reported_with_the_scenario() -> None:
    run = object.__new__(_FixtureRun)
    run.runtime = _runtime(ownership._OwnershipProbeError("not_sent", "ownership probe request was not sent"), _failing_reset)
    run.runtime._ownership_outcome("timeout", time.monotonic() + 60)
    run.errors = []
    run.ownership = {"timeout": {"available": False, "orphan_count": None}}

    run._name_unmeasured_ownership()

    assert [(entry["phase"], entry["code"]) for entry in run.errors] == [
        ("ownership:timeout", "not_sent"),
        ("ownership:timeout:recovery", "OSError"),
    ]
    assert run.errors[1]["message"].startswith("the server would not stop (after ")


def _names_ownership_probe(node: ast.Call) -> bool:
    return any(isinstance(arg, ast.Constant) and str(arg.value).startswith("ownership probe") for arg in node.args)


def _is_bare_probe_error(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or getattr(node.func, "id", None) != "RuntimeError":
        return False
    return _names_ownership_probe(node)


def _is_suppress(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "suppress"


def _runner_sources() -> list[Path]:
    return [BENCHMARK_ROOT / "run_code_navigation.py", *sorted(BENCHMARK_ROOT.glob("navigation_benchmark_*.py"))]


def _runner_nodes() -> list[tuple[str, ast.AST]]:
    return [
        (source.name, node)
        for source in _runner_sources()
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
    ]


def test_the_scan_reads_the_runner_and_every_part_it_was_split_into() -> None:
    names = set(source.name for source in _runner_sources())
    assert set(["run_code_navigation.py", "navigation_benchmark_ownership.py"]) <= names


def test_no_ownership_failure_is_a_bare_runtime_error_or_swallowed() -> None:
    nodes = _runner_nodes()

    bare = [(name, node.lineno) for name, node in nodes if _is_bare_probe_error(node)]
    swallowed = [(name, node.lineno) for name, node in nodes if _is_suppress(node)]

    assert (bare, swallowed) == ([], [])
