"""Helpers shared by test_shell_functions_stay_simple.py and the test files importing them."""
from __future__ import annotations

import tree_sitter_bash
from tree_sitter import Language, Node, Parser

MAX_CCN = 5


MAX_NESTING = 2


DECISIONS = frozenset({
    "if_statement", "elif_clause", "while_statement", "for_statement",
    "c_style_for_statement", "&&", "||",
})


NESTING = frozenset({"if_statement", "while_statement", "for_statement", "c_style_for_statement"})


PARSER = Parser(Language(tree_sitter_bash.language()))


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


def _shape_violations(source: bytes) -> list[str]:
    return [
        f"{_name(fn)}: {reason}\n{fn.text.decode()}"
        for fn in _functions(source) for reason in _shape_reasons(fn)
    ]


def _script_body_violations(source: bytes) -> list[str]:
    """The statements outside every function are one more unit under the same ceiling."""
    body = PARSER.parse(source).root_node
    measured = {"ccn": (_ccn(body), MAX_CCN), "nesting": (_nesting(body), MAX_NESTING)}
    reasons = [f"{label}={value} > {limit}" for label, (value, limit) in measured.items() if value > limit]
    return [f"script body: {reason}" for reason in [*reasons, *_shape_reasons(body)]]


def _shape_reasons(function: Node) -> list[str]:
    nodes = _own_nodes(function)
    measured = {"if count": (sum(n.type == "if_statement" for n in nodes), 2),
                "else-if chains": (sum(n.type == "elif_clause" for n in nodes), 0)}
    return [f"{label}={value} > {limit}" for label, (value, limit) in measured.items() if value > limit]
