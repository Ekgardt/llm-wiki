"""Every shell function the repository ships stays under the complexity ceiling.

The machine gate that measures an agent's edits skips `.sh`, and CI checked
`install.sh` only with `bash -n` and shellcheck, so installer functions grew to a
cyclomatic complexity of 7 to 14 unseen. lizard has no shell reader and reads a
`.sh` file as C, which misses `elif`, `until` and every `case` arm; this file
parses the shell with tree-sitter-bash, a dependency the code graph already
carries, and counts McCabe's decision points the way the shell spells them:
each `if`, `elif`, loop, `&&` and `||` adds one, and a `case` adds one per arm
except an explicit `*)` default (NIST SP 500-235, section 4.2).
See docs/research/2026-09-27-shell-functions-are-measured-too.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tree_sitter_bash
from tree_sitter import Language, Node, Parser

ROOT = Path(__file__).resolve().parents[1]
MAX_CCN = 5
MAX_NESTING = 2
CODE_ZONES = ("scripts", "tests", "docs", "skills", "rules", "integrations", "benchmark")
DECISIONS = frozenset({
    "if_statement", "elif_clause", "while_statement", "for_statement",
    "c_style_for_statement", "&&", "||",
})
NESTING = frozenset({"if_statement", "while_statement", "for_statement", "c_style_for_statement"})
PARSER = Parser(Language(tree_sitter_bash.language()))


def _shell_files() -> list[Path]:
    nested = [path for zone in CODE_ZONES for path in (ROOT / zone).rglob("*.sh")]
    return sorted([*ROOT.glob("*.sh"), *nested])


def _own_nodes(node: Node) -> list[Node]:
    """Every node inside a function body, but not inside a function nested in it."""
    found = []
    for child in node.children:
        if child.type != "function_definition":
            found += [child, *_own_nodes(child)]
    return found


def _functions(source: bytes) -> list[Node]:
    return [node for node in _all_nodes(PARSER.parse(source).root_node) if node.type == "function_definition"]


def _all_nodes(node: Node) -> list[Node]:
    return [node, *(found for child in node.children for found in _all_nodes(child))]


def _is_catch_all(arm: Node) -> bool:
    patterns = arm.children_by_field_name("value")
    return len(patterns) == 1 and patterns[0].text == b"*"


def _is_decision(node: Node) -> bool:
    """A `case` arm is one outcome of a multiway decision; an explicit `*)` is its default."""
    if node.type == "case_item":
        return not _is_catch_all(node)
    return node.type in DECISIONS


def _ccn(function: Node) -> int:
    return 1 + sum(1 for node in _own_nodes(function) if _is_decision(node))


def _nesting(node: Node, depth: int = 0) -> int:
    inner = depth + (node.type in NESTING)
    children = [child for child in node.children if child.type != "function_definition"]
    return max([inner, *(_nesting(child, inner) for child in children)])


def _name(function: Node) -> str:
    return function.child_by_field_name("name").text.decode()


def _violations(source: bytes) -> dict[str, tuple[int, int]]:
    measured = {_name(fn): (_ccn(fn), _nesting(fn)) for fn in _functions(source)}
    return {name: pair for name, pair in measured.items() if pair[0] > MAX_CCN or pair[1] > MAX_NESTING}


def test_the_installer_is_among_the_measured_files() -> None:
    assert ROOT / "install.sh" in _shell_files()


@pytest.mark.parametrize("path", _shell_files(), ids=lambda path: path.relative_to(ROOT).as_posix())
def test_every_shipped_shell_function_is_under_the_ceiling(path: Path) -> None:
    assert _violations(path.read_bytes()) == {}
    assert _shape_violations(path.read_bytes()) == []


def _shape_violations(source: bytes) -> list[str]:
    return [
        f"{_name(fn)}: {reason}\n{fn.text.decode()}"
        for fn in _functions(source) for reason in _shape_reasons(fn)
    ]


def _shape_reasons(function: Node) -> list[str]:
    nodes = _own_nodes(function)
    measured = {"if count": (sum(n.type == "if_statement" for n in nodes), 2),
                "else-if chains": (sum(n.type == "elif_clause" for n in nodes), 0)}
    return [f"{label}={value} > {limit}" for label, (value, limit) in measured.items() if value > limit]


@pytest.mark.parametrize("body", [
    "if a; then :; fi; if b; then :; fi; if c; then :; fi",
    "if a; then :; elif b; then :; fi",
])
def test_forbidden_shell_branch_shapes_are_detected(body):
    assert _shape_violations(f"bad() {{ {body}; }}".encode())


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('if a; then :; elif b; then :; fi', 3),
        ('until a; do :; done; while b; do :; done', 3),
        ('case "$1" in a) :;; b) :;; *) :;; esac', 3),
        ('case "$1" in a) :;; b|c) :;; esac', 3),
        ('a && b || c; [[ -n "$x" && -z "$y" ]]', 4),
        ('for x in 1 2; do :; done; for ((i=0; i<2; i++)); do :; done', 3),
        ('inner() { if a; then :; fi; }', 1),
    ],
)
def test_each_shell_decision_point_is_counted(body: str, expected: int) -> None:
    functions = _functions(f"outer() {{\n{body}\n}}\n".encode())
    assert _ccn(next(fn for fn in functions if _name(fn) == "outer")) == expected


def test_nesting_counts_blocks_inside_blocks() -> None:
    source = b"deep() {\n  for x in 1; do\n    while a; do\n      if b; then :; fi\n    done\n  done\n}\n"
    assert _violations(source) == {"deep": (4, 3)}
