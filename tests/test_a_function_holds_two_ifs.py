"""A function holds at most two `if` statements (law 5).

The rule counts every `if` in one function, nested ones included; a nested
function is its own function. Audit 2026-09-27 found 93 functions over it in
`scripts/` (and 36 in `tests/` and `benchmark/`); all were split, and this guard holds every module to the
rule. See `docs/research/2026-09-27-a-function-holds-two-ifs.md` and
`docs/research/2026-09-27-every-function-holds-two-ifs.md`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import lizard
import pytest

ROOT = Path(__file__).resolve().parents[1]
MAX_IFS = 2
MAX_CCN = 5
MAX_NESTING = 2
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

def _ifs(node: ast.AST) -> int:
    """The `if` statements this scope owns, without those of nested scopes."""
    children = [child for child in ast.iter_child_nodes(node) if not isinstance(child, _SCOPES)]
    return sum(isinstance(child, ast.If) + _ifs(child) for child in children)


def _functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda]:
    return [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]


def _name(node: ast.AST) -> str:
    return getattr(node, "name", f"<lambda at {node.lineno}>")


def _over_the_rule(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    name = path.relative_to(ROOT).as_posix()
    return {f"{name}::{_name(node)}" for node in _functions(tree) if _ifs(node) > MAX_IFS}


def test_the_counter_sees_nested_ifs_and_skips_nested_functions() -> None:
    source = "def f(x):\n    if x:\n        if x:\n            pass\n    def g():\n        if x: pass\n    if x: pass\n"
    function = ast.parse(source).body[0]
    assert _ifs(function) == 3


def _code_files() -> list[Path]:
    """The project's Python; `tests/fixtures/` holds inputs, some deliberately unparseable."""
    files = [path for folder in ("scripts", "tests", "benchmark") for path in (ROOT / folder).rglob("*.py")]
    return sorted(path for path in files if "fixtures" not in path.relative_to(ROOT).parts)


def test_no_function_holds_more_than_two_ifs() -> None:
    offenders = set().union(*(_over_the_rule(path) for path in _code_files()))
    assert offenders == set()


def _own_children(node: ast.AST) -> list[ast.AST]:
    return [child for child in ast.iter_child_nodes(node) if not isinstance(child, _SCOPES)]


def _owned_nodes(node: ast.AST) -> list[ast.AST]:
    children = _own_children(node)
    return [*children, *(item for child in children for item in _owned_nodes(child))]


def _depth(node: ast.AST, depth: int = 0) -> int:
    current = depth + isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While))
    return max([current, *(_depth(child, current) for child in _own_children(node))])


def _is_chain(node: ast.AST) -> bool:
    if not isinstance(node, ast.If) or not node.orelse:
        return False
    return isinstance(node.orelse[0], ast.If)


def _complex_ternary(node: ast.AST) -> bool:
    if not isinstance(node, ast.IfExp):
        return False
    return any(isinstance(child, (ast.IfExp, ast.BoolOp)) for child in _owned_nodes(node))


def _shape_problems(source: str) -> list[str]:
    functions = _functions(ast.parse(source))
    return [problem for function in functions for problem in _function_shape(source, function)]


def _function_shape(source: str, function) -> list[str]:
    nodes = _owned_nodes(function)
    measured = {
        "if count": (_ifs(function), MAX_IFS),
        "nesting": (_depth(function), MAX_NESTING),
        "if/else-if chains": (sum(map(_is_chain, nodes)), 0),
        "complex ternaries": (sum(map(_complex_ternary, nodes)), 0),
        "lambda CCN": (_lambda_ccn(function), MAX_CCN),
    }
    failures = [(label, value, bound) for label, (value, bound) in measured.items() if value > bound]
    if not failures:
        return []
    code = ast.get_source_segment(source, function)
    return [
        f"{_name(function)}: {label}={value} > {bound}\n{code}"
        for label, value, bound in failures
    ]


def _expression_decisions(node: ast.AST) -> int:
    if isinstance(node, ast.BoolOp):
        return len(node.values) - 1
    if isinstance(node, ast.comprehension):
        return 1 + len(node.ifs)
    return int(isinstance(node, ast.IfExp))


def _lambda_ccn(node: ast.AST) -> int:
    if not isinstance(node, ast.Lambda):
        return 1
    return 1 + sum(_expression_decisions(child) for child in _owned_nodes(node))


def _ccn_problems(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    functions = lizard.analyze_file(str(path)).function_list
    return [
        f"{path}:{fn.start_line} {fn.name}: CCN={fn.cyclomatic_complexity}\n"
        + "\n".join(lines[fn.start_line - 1:fn.end_line])
        for fn in functions if fn.cyclomatic_complexity > MAX_CCN
    ]


def test_every_python_function_obeys_the_branching_shapes() -> None:
    problems = {
        str(path.relative_to(ROOT)): _shape_problems(path.read_text(encoding="utf-8"))
        for path in _code_files()
    }
    assert {path: findings for path, findings in problems.items() if findings} == {}


def test_every_python_and_javascript_function_meets_ccn() -> None:
    javascript = [path for zone in ("scripts", "tests", "integrations") for path in (ROOT / zone).rglob("*.js")]
    problems = [problem for path in [*_code_files(), *javascript] for problem in _ccn_problems(path)]
    assert problems == []


@pytest.mark.parametrize("body", [
    "if x: return 1\n    elif y: return 2",
    "for x in xs:\n        while x:\n            if y: break",
    "return a if x and y else b",
    "return (a if y else b) if x else c",
])
def test_each_forbidden_shape_is_detected(body):
    assert _shape_problems("def planted(x, y, xs, a, b, c):\n    " + body + "\n")


def test_ccn_failure_reports_the_function_measurement_and_code(tmp_path):
    path = tmp_path / "planted.py"
    source = "def too_complex(a, b, c, d, e, f):\n    return a and b and c and d and e and f\n"
    path.write_text(source)
    problems = _ccn_problems(path)
    assert len(problems) == 1
    assert "CCN=6" in problems[0]
    assert source.strip() in problems[0]


@pytest.mark.parametrize("expression", [
    "a and b and c and d and e and f",
    "a if b and c else d",
])
def test_lambda_handlers_are_measured_separately(expression):
    problems = _shape_problems(f"handler = lambda a, b, c, d, e, f: {expression}")
    assert problems
    assert "<lambda at 1>" in problems[0]
