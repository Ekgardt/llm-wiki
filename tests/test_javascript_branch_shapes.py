"""JavaScript callbacks obey the same shape rules as named functions."""

from pathlib import Path

import pytest
import tree_sitter_javascript
from tree_sitter import Language, Node, Parser

ROOT = Path(__file__).resolve().parents[1]
PARSER = Parser(Language(tree_sitter_javascript.language()))
FUNCTIONS = {"function_declaration", "function_expression", "arrow_function", "method_definition",
             "generator_function", "generator_function_declaration"}
NESTING = {"if_statement", "for_statement", "for_in_statement", "while_statement", "do_statement"}


def _nodes(node: Node) -> list[Node]:
    return [node, *(item for child in node.children for item in _nodes(child))]


def _owned(node: Node) -> list[Node]:
    children = [child for child in node.children if child.type not in FUNCTIONS]
    return [*children, *(item for child in children for item in _owned(child))]


def _depth(node: Node, level: int = 0) -> int:
    current = level + (node.type in NESTING)
    children = [child for child in node.children if child.type not in FUNCTIONS]
    return max([current, *(_depth(child, current) for child in children)])


def _chain(node: Node) -> bool:
    alternative = node.child_by_field_name("alternative")
    if node.type != "if_statement" or alternative is None:
        return False
    return any(child.type == "if_statement" for child in alternative.children)


def _complex_ternary(node: Node) -> bool:
    if node.type != "ternary_expression":
        return False
    return any(child.type in {"ternary_expression", "&&", "||", "??"} for child in _owned(node))


def _shape(function: Node) -> list[str]:
    nodes = _owned(function)
    measured = {"ifs": (sum(n.type == "if_statement" for n in nodes), 2),
                "nesting": (_depth(function), 2),
                "chains": (sum(map(_chain, nodes)), 0),
                "complex ternaries": (sum(map(_complex_ternary, nodes)), 0)}
    return [f"{label}={value} > {bound}\n{function.text.decode()}"
            for label, (value, bound) in measured.items() if value > bound]


def _problems(source: bytes) -> list[str]:
    tree = PARSER.parse(source)
    assert not tree.root_node.has_error, "JavaScript parse failed"
    functions = [node for node in _nodes(tree.root_node) if node.type in FUNCTIONS]
    return [problem for function in functions for problem in _shape(function)]


def _javascript_files() -> list[Path]:
    zones = ("scripts", "tests", "integrations", "benchmark", "docs", "skills", "rules")
    return [*ROOT.glob("*.js"), *(p for zone in zones for p in (ROOT / zone).rglob("*.js"))]


def test_javascript_shapes():
    problems = {str(p.relative_to(ROOT)): _problems(p.read_bytes()) for p in _javascript_files()}
    assert {path: found for path, found in problems.items() if found} == {}


@pytest.mark.parametrize("body", [
    "if(a) {} if(b) {} if(c) {}",
    "if(a) {} else if(b) {}",
    "for(;;) { while(a) { if(b) {} } }",
    "return a && b ? c : d",
    "return a ? (b ? c : d) : e",
])
def test_javascript_forbidden_shapes_are_detected(body):
    assert _problems(f"const callback = () => {{ {body} }}".encode())


def test_nested_function_has_its_own_scope():
    assert _problems(b"function outer() { if(a){} if(b){}; const inner = () => { if(c){} }; }") == []
