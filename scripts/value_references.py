"""Which names the corpus loads as values, and which definitions it hands away.

`find_dead_code` asked one question — does any call site write this name? — and
a symbol handed over as a *value* is never written at a call site. A thread
target, a registry entry, `set_defaults(handler=...)`, a dict of handlers: the
name is loaded, the object travels, the call happens somewhere the extractor
cannot follow. No `CALLS` edge is produced, so the symbol looked uncalled.

Measured 2026-08-29 against the active generation: of 461 symbols the answer
called `zero_confirmed_incoming_calls`, **402 are loaded as a value somewhere
in the same corpus** — 87% of the strongest verdict was false. The tool refuted
itself inside one run: `_architecture_dependencies` and `_architecture_symbol`
were listed dead while sitting in `_ARCHITECTURE_MODE_QUERIES`, a dict the same
run executed from.

Two more classes are invisible to name analysis *by construction*, exactly like
the protocol dunders `code_graph._protocol_invoked` already excludes:

* a **decorated** definition — the decorator receives the function object at
  import time and may keep it (`@pytest.fixture`, `@server.list_resources()`,
  `@app.route(...)`). Vulture ships `--ignore-decorators` for this reason.
* a **method of a class that declares a base** — the base dispatches it
  (`ast.NodeVisitor.visit` reaches `visit_Call` through `getattr`, never by
  name; `io.RawIOBase` calls `readinto`). Measured here: 17 of the 59 remaining
  candidates were these, 14 of them `visit_*`.

Everything below reads bytes the generation already stores. Nothing changes
extraction, so no published generation is invalidated (`NEW-81`).

Sources: `docs/research/2026-08-29-a-name-loaded-is-a-name-used.md`.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass

#: Decorators that wrap a definition for the attribute lookup that already
#: names it, rather than handing it to a registry. Anything else is treated as
#: a hand-off, because "wraps" and "registers" cannot be told apart statically
#: and only one of the two mistakes calls live code dead.
DESCRIPTOR_DECORATORS = frozenset(
    {
        "abc.abstractmethod",
        "abstractmethod",
        "classmethod",
        "functools.cached_property",
        "cached_property",
        "override",
        "property",
        "staticmethod",
        "typing.overload",
        "overload",
    }
)

_IDENTIFIER = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*")
_IDENTIFIER_TEXT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DEFINITION = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True, slots=True)
class ReferenceIndex:
    """What the corpus names, and which definitions it never has to name."""

    value_names: frozenset[str]
    dispatched: frozenset[tuple[str, str, int]]
    parsed_sources: int
    lexical_sources: int

    def names_a_value(self, name: str) -> bool:
        """True when some source names `name` anywhere that is not a call site."""
        return name in self.value_names

    def is_dispatched(self, path: str, name: str, line: int) -> bool:
        """True when the definition at this exact place is handed to a caller."""
        return (path, name, line) in self.dispatched

    def as_report(self) -> dict[str, int]:
        """The coverage this index was built from, for the answer's report."""
        return {
            "reference_value_names": len(self.value_names),
            "reference_dispatched_definitions": len(self.dispatched),
            "reference_parsed_sources": self.parsed_sources,
            "reference_lexical_sources": self.lexical_sources,
        }


EMPTY_INDEX = ReferenceIndex(frozenset(), frozenset(), 0, 0)


def _in_load_context(node: ast.expr) -> bool:
    return isinstance(node.ctx, ast.Load)  # type: ignore[attr-defined]


def _name_load(node: ast.Name) -> str | None:
    return node.id if _in_load_context(node) else None


def _attribute_load(node: ast.Attribute) -> str | None:
    return node.attr if _in_load_context(node) else None


def _constant_identifier(node: ast.Constant) -> str | None:
    """An identifier spelled as a string is how dynamic dispatch names a symbol.

    `{"Windows": "_windows_process_start_identity"}` and
    `{"project_lease": "_check_lease_precondition"}` are both live tables in
    this repository, resolved through `getattr`. Measured 2026-08-29: reading
    string constants rescues 7 symbols that a load-only sweep still called
    dead, and moves 4 genuinely unused ones from the dead verdict to doubt,
    because a fixture spells their names. That trade is deliberate — the
    failure being repaired is calling live code dead, and a symbol moved to
    doubt is still in the answer. A docstring is not an identifier, so prose
    that merely mentions a name does not rescue it.
    """
    if not isinstance(node.value, str):
        return None
    return node.value if _IDENTIFIER_TEXT.fullmatch(node.value) else None


#: What each node type contributes to "something names this symbol". Keyed by
#: exact type so a subclass never silently inherits another node's reading.
_NAME_READERS = {
    ast.Name: _name_load,
    ast.Attribute: _attribute_load,
    ast.Constant: _constant_identifier,
}


def _loaded_name(node: ast.AST) -> str | None:
    """The name this node names without calling it, or None.

    A bare `ast.Name` load is `handler` in `Thread(target=handler)`; an
    `ast.Attribute` load is `handler` in `registry.handler`; a string constant
    is `"handler"` in a dispatch table. None of the three is a call site, and
    all three are exactly what a call-site-only reader cannot see.
    """
    reader = _NAME_READERS.get(type(node))
    if reader is None:
        return None
    return reader(node)


def _value_names(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        name = _loaded_name(node)
        if name is not None:
            found.add(name)
    return found


def _decorator_expression(decorator: ast.expr) -> str:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return ast.unparse(target)


def _hands_over(decorator: ast.expr) -> bool:
    return _decorator_expression(decorator) not in DESCRIPTOR_DECORATORS


def _registered(definition: ast.AST) -> bool:
    """True when a decorator may keep this function object."""
    return any(_hands_over(item) for item in definition.decorator_list)  # type: ignore[attr-defined]


def _declares_a_base(owner: ast.ClassDef | None) -> bool:
    """True when the owning class inherits, so a base may dispatch the method."""
    if owner is None:
        return False
    return any(ast.unparse(base) != "object" for base in owner.bases)


def _dispatched_here(definition: ast.AST, owner: ast.ClassDef | None) -> bool:
    if _registered(definition):
        return True
    return _declares_a_base(owner)


def _definition_key(path: str, definition: ast.AST) -> tuple[str, str, int]:
    return (path, definition.name, definition.lineno)  # type: ignore[attr-defined]


def _collect_definitions(
    node: ast.AST, path: str, owner: ast.ClassDef | None, found: set
) -> None:
    for child in ast.iter_child_nodes(node):
        _visit_definition(child, path, owner, found)


def _visit_definition(
    child: ast.AST, path: str, owner: ast.ClassDef | None, found: set
) -> None:
    if isinstance(child, ast.ClassDef):
        _collect_definitions(child, path, child, found)
        return
    if isinstance(child, _DEFINITION):
        _record_definition(child, path, owner, found)
        return
    _collect_definitions(child, path, owner, found)


def _record_definition(
    child: ast.AST, path: str, owner: ast.ClassDef | None, found: set
) -> None:
    if _dispatched_here(child, owner):
        found.add(_definition_key(path, child))
    _collect_definitions(child, path, None, found)


def _parsed(content: bytes) -> ast.Module | None:
    try:
        return ast.parse(content)
    except (SyntaxError, ValueError, RecursionError):
        return None


def lexical_names(content: bytes) -> set[str]:
    """Every identifier in a source that would not parse.

    Wider than the AST answer on purpose. A source the reader cannot parse must
    never let the tool say "nothing names it": widening loses candidates, and
    the opposite mistake calls live code dead. Measured here — one deliberately
    broken fixture out of 407 stored Python sources.
    """
    return {
        match.group().decode("utf-8", "replace")
        for match in _IDENTIFIER.finditer(content)
    }


def _add_source(
    source: tuple[str, bytes], values: set[str], dispatched: set
) -> bool:
    """Fold one source in; True when it parsed, False when it fell back."""
    path, content = source
    tree = _parsed(content)
    if tree is None:
        values.update(lexical_names(content))
        return False
    values.update(_value_names(tree))
    _collect_definitions(tree, path, None, dispatched)
    return True


def build_reference_index(sources: Iterable[tuple[str, bytes]]) -> ReferenceIndex:
    """Read every stored Python source once and answer both questions from it."""
    values: set[str] = set()
    dispatched: set[tuple[str, str, int]] = set()
    parsed = 0
    total = 0
    for source in sources:
        total += 1
        parsed += int(_add_source(source, values, dispatched))
    return ReferenceIndex(
        frozenset(values), frozenset(dispatched), parsed, total - parsed
    )
