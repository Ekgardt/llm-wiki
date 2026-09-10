"""The waits are named, ordered, and scaled at the invocation, never literal."""

from __future__ import annotations

import ast
import importlib
import os
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
