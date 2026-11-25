"""The waits are named, ordered, and scaled at the invocation, never literal."""

from __future__ import annotations

import ast
import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_the_two_waits_are_ordered_and_the_pause_outlives_them() -> None:
    from tests import slow_machine

    assert 0 < slow_machine.SHORT_TIMEOUT < slow_machine.LONG_TIMEOUT
    assert slow_machine.PAUSE_TIMEOUT > slow_machine.LONG_TIMEOUT


@pytest.mark.parametrize(("scale", "expected"), [("2", 600.0), ("0.5", 150.0)])
def test_a_loaded_machine_scales_every_wait_at_the_invocation(scale, expected) -> None:
    code = "from tests import slow_machine; print(slow_machine.LONG_TIMEOUT)"
    # The child inherits the environment: a Windows Python started without
    # SYSTEMROOT dies before its first import (python/cpython#105436).
    env = {**os.environ, "LLM_WIKI_TEST_TIMEOUT_SCALE": scale, "PYTHONPATH": str(ROOT)}
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert float(result.stdout.strip()) == expected


def test_a_scale_that_is_not_positive_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests import slow_machine

    monkeypatch.setenv("LLM_WIKI_TEST_TIMEOUT_SCALE", "0")
    with pytest.raises(ValueError, match="must be positive"):
        importlib.reload(slow_machine)
    monkeypatch.delenv("LLM_WIKI_TEST_TIMEOUT_SCALE")
    importlib.reload(slow_machine)


# --- no test carries a literal hang bound -----------------------------------

_BOUNDED_CALLS = frozenset({"join", "result", "get", "wait", "acquire"})
_POSITIONAL_BOUND = frozenset({"join", "wait", "acquire"})


def _is_number(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and type(node.value) in (int, float)


def _keyword_bound(call: ast.Call) -> ast.Constant | None:
    for keyword in call.keywords:
        if keyword.arg == "timeout" and _is_number(keyword.value):
            return keyword.value
    return None


def _positional_bound(call: ast.Call) -> ast.Constant | None:
    if call.func.attr not in _POSITIONAL_BOUND or not call.args:
        return None
    first = call.args[0]
    return first if _is_number(first) else None


def _literal_bound(call: ast.Call) -> ast.Constant | None:
    """The literal number bounding `call`, when it is one of the waiting calls."""
    if not isinstance(call.func, ast.Attribute):
        return None
    if call.func.attr not in _BOUNDED_CALLS:
        return None
    keyword = _keyword_bound(call)
    return keyword if keyword is not None else _positional_bound(call)


def _compares_with_false_or_none(node: ast.Compare) -> bool:
    return any(isinstance(c, ast.Constant) and c.value in (False, None) for c in node.comparators)


def _is_raises(node: ast.AST) -> bool:
    if isinstance(node, ast.With):
        return any("raises" in ast.dump(item.context_expr) for item in node.items)
    return isinstance(node, ast.Call) and "raises" in ast.dump(node.func)


def _expects_the_bound_to_elapse(node: ast.AST) -> bool:
    """`not x.wait(1)`, `x.wait(1) is False`, `pytest.raises(...)`: the bound is the point."""
    if isinstance(node, ast.UnaryOp):
        return isinstance(node.op, ast.Not)
    if isinstance(node, ast.Compare):
        return _compares_with_false_or_none(node)
    return _is_raises(node)


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _is_negative(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    node: ast.AST = call
    while node in parents and not isinstance(node, (ast.FunctionDef, ast.Module)):
        node = parents[node]
        if _expects_the_bound_to_elapse(node):
            return True
    return False


def _asserted_true(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    """`assert e.wait(n)` or `assert e.wait(n) and …`: the test expects the event."""
    parent = parents.get(call)
    if isinstance(parent, ast.BoolOp):
        parent = parents.get(parent)
    return isinstance(parent, ast.Assert)


def _is_hang_bound(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    """A `wait` is a hang bound only when the test asserts the event arrives; a
    discarded, branched-on or assigned `wait` is the test's own choice of pause.
    `join`, `result` and `get` bounds are hang bounds unless the test expects
    them to elapse."""
    if call.func.attr == "wait":
        return _asserted_true(call, parents)
    return not _is_negative(call, parents)


def _literal_hang_bounds(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = _parents(tree)
    found = []
    for node in ast.walk(tree):
        bound = None if not isinstance(node, ast.Call) else _literal_bound(node)
        if bound is not None and _is_hang_bound(node, parents):
            found.append(f"{path.name}:{node.lineno} {node.func.attr}({bound.value})")
    return found


def test_no_test_carries_a_literal_hang_bound() -> None:
    """A positive wait names SHORT_TIMEOUT or LONG_TIMEOUT; a literal is a hang bound sized for one machine."""
    offenders: list[str] = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        offenders.extend(_literal_hang_bounds(path))

    assert offenders == []


# --- no test gives real work a literal deadline -------------------------------
#
# `deadline=time.monotonic() + 5` handed to a directory walk failed a clean full
# run on 2026-09-26 (the walk did not finish in 5 s under load) while passing
# alone. A deadline bounding work the test expects to finish names SHORT_TIMEOUT
# or LONG_TIMEOUT; a literal one is allowed only where the test is about time
# running out, which its function name says. Research:
# docs/research/2026-09-26-no-test-gives-real-work-a-literal-deadline.md

_ABOUT_TIME = re.compile(
    r"deadline|timeout|timed|expire|late|elapse|budget|hang|slow|stall|stuck|overrun|past_due|in_time|cancel|interrupt",
    re.IGNORECASE,
)


_LITERAL_DEADLINE = re.compile(r"^time\.monotonic\(\) \+ (\d+(?:\.\d+)?)$")


def _is_literal_deadline(keyword: ast.keyword) -> bool:
    """`deadline=time.monotonic() + N` with N of a second or more."""
    match = _LITERAL_DEADLINE.match(ast.unparse(keyword.value))
    return keyword.arg == "deadline" and match is not None and float(match.group(1)) >= 1


def _speaks_of_time(node: ast.AST) -> bool:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    return _ABOUT_TIME.search(node.name) is not None


def _call_deadlines(node: ast.AST) -> list[int]:
    if not isinstance(node, ast.Call):
        return []
    return [keyword.value.lineno for keyword in node.keywords if _is_literal_deadline(keyword)]


def _literal_deadlines(node: ast.AST, about_time: bool) -> list[int]:
    about_time = about_time or _speaks_of_time(node)
    found = [] if about_time else _call_deadlines(node)
    for child in ast.iter_child_nodes(node):
        found += _literal_deadlines(child, about_time)
    return found


def test_no_test_gives_real_work_a_literal_deadline() -> None:
    offenders = {
        path.name: _literal_deadlines(ast.parse(path.read_text(encoding="utf-8")), False)
        for path in sorted((ROOT / "tests").glob("*.py"))
    }

    assert {name: lines for name, lines in offenders.items() if lines} == {}


# --- no test times real work against a literal margin -------------------------
#
# `assert started < deadline <= started + 0.11` failed on a hosted Windows runner
# (run 45517849417, 2026-11-25) by one 15.6 ms clock step with unchanged code: a
# margin on the real clock measures the machine. A test controls the clock
# instead (`tests/clocks.py`) or bounds a hang with `run_within_hang_bound`; a
# margin may only be a named wait from `slow_machine`, scaled at the invocation.

_CLOCKS = frozenset({"monotonic", "perf_counter", "time"})
_ORDERINGS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)


def _is_clock_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    owner = node.func.value
    return node.func.attr in _CLOCKS and isinstance(owner, ast.Name) and owner.id == "time"


def _terms(node: ast.AST) -> list[ast.AST]:
    """The summands of an expression: a margin is added or subtracted, never scaled."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
        return [*_terms(node.left), *_terms(node.right)]
    return [node]


def _leaf_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Subscript):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _reads_clock(node: ast.AST, readings: set[str]) -> bool:
    return any(_is_clock_call(term) or _leaf_name(term) in readings for term in _terms(node))


def _is_literal_margin(term: ast.AST) -> bool:
    return _is_number(term) and term.value != 0  # type: ignore[attr-defined]


def _assignment(node: ast.AST) -> tuple[ast.AST, ast.AST] | None:
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        return node.targets[0], node.value
    return None


def _appended(node: ast.AST) -> tuple[ast.AST, ast.AST] | None:
    if not isinstance(node, ast.Call) or not node.args:
        return None
    if isinstance(node.func, ast.Attribute) and node.func.attr == "append":
        return node.func.value, node.args[0]
    return None


def _assigned_value(node: ast.AST) -> tuple[ast.AST, ast.AST] | None:
    """`name = value` or `name.append(value)`: where a clock reading is kept."""
    return _assignment(node) or _appended(node)


def _clock_readings(function: ast.AST) -> set[str]:
    readings: set[str] = set()
    for node in ast.walk(function):
        assigned = _assigned_value(node)
        if assigned is not None and _reads_clock(assigned[1], readings):
            readings.add(_leaf_name(assigned[0]) or "")
    return readings


def _is_ordering(compare: ast.Compare) -> bool:
    return all(isinstance(op, _ORDERINGS) for op in compare.ops)


def _has_literal_margin(operands: list[ast.expr]) -> bool:
    return any(_is_literal_margin(term) for operand in operands for term in _terms(operand))


def _is_stopwatch(compare: ast.Compare, readings: set[str]) -> bool:
    if not _is_ordering(compare):
        return False
    operands = [compare.left, *compare.comparators]
    timed = any(_reads_clock(operand, readings) for operand in operands)
    return timed and _has_literal_margin(operands)


def _asserted(function: ast.AST) -> list[ast.expr]:
    return [node.test for node in ast.walk(function) if isinstance(node, ast.Assert)]


def _compares(node: ast.AST) -> list[ast.Compare]:
    return [child for child in ast.walk(node) if isinstance(child, ast.Compare)]


def _stopwatches(function: ast.AST) -> list[int]:
    readings = _clock_readings(function)
    compares = [compare for test in _asserted(function) for compare in _compares(test)]
    return sorted({compare.lineno for compare in compares if _is_stopwatch(compare, readings)})


def _functions(tree: ast.AST) -> list[ast.AST]:
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef)
    return [node for node in ast.walk(tree) if isinstance(node, kinds)]


def _file_stopwatches(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return sorted({line for function in _functions(tree) for line in _stopwatches(function)})


@pytest.mark.parametrize(
    "source",
    [
        "def t():\n    started = time.monotonic()\n    work()\n"
        "    assert time.monotonic() - started < 0.2\n",
        "def t():\n    elapsed = time.perf_counter() - started\n"
        "    assert (elapsed < 0.5, ok) == (True, True)\n",
        "def t():\n    spent.append(time.monotonic() - begun)\n    assert spent[0] < 0.05\n",
        "def t():\n    started = time.monotonic()\n    assert started < seen <= started + 0.11\n",
        "def t():\n    deadline = time.monotonic() + 1\n"
        "    assert time.monotonic() <= deadline + 0.2\n",
    ],
)
def test_the_stopwatch_guard_recognizes_a_literal_margin_on_the_real_clock(source: str) -> None:
    function = ast.parse(source).body[0]

    assert _stopwatches(function) != []


@pytest.mark.parametrize(
    "source",
    [
        "def t():\n    assert time.monotonic() - started < SHORT_TIMEOUT / 2\n",
        "def t():\n    now = time.monotonic()\n    assert next_deadline() == now - 1\n",
        "def t():\n    assert 0 <= timeout <= deadline - time.monotonic()\n",
        "def t():\n    clock = VirtualClock(100.0)\n    assert clock.now - 100.0 <= 0.1\n",
    ],
)
def test_the_stopwatch_guard_lets_named_waits_exact_values_and_controlled_clocks_pass(
    source: str,
) -> None:
    function = ast.parse(source).body[0]

    assert _stopwatches(function) == []


def test_no_test_times_real_work_against_a_literal_margin() -> None:
    paths = sorted((ROOT / "tests").glob("test_*.py"))
    offenders = {path.name: _file_stopwatches(path) for path in paths}

    assert {name: lines for name, lines in offenders.items() if lines} == {}
