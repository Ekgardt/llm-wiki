"""Every name a script imports from a first-party module is bound by that module.

An import inside a function runs only when the function does, so a name removed from
its provider fails in production, not at import. The roots change of 2026-11-10 left
two such imports of `compile_pipeline.ROOT` in `compile_controller`.

docs/research/2026-11-10-roots-are-read-at-call-time.md
"""
from __future__ import annotations

import ast
import functools
import sys
from pathlib import Path

import pytest

from tests.script_sources import SCRIPTS

_NESTED_BLOCKS = (ast.If, ast.Try, ast.With)


def _module_file(module: str) -> Path | None:
    base = SCRIPTS.joinpath(*module.split("."))
    candidates = (base.with_suffix(".py"), base / "__init__.py")
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _block_statements(statement: ast.stmt) -> list[ast.stmt]:
    blocks = [statement.body, getattr(statement, "orelse", []), getattr(statement, "finalbody", [])]
    handlers = [handler.body for handler in getattr(statement, "handlers", [])]
    return [inner for block in [*blocks, *handlers] for inner in block]


def _module_level(statements: list[ast.stmt]):
    for statement in statements:
        yield statement
        if isinstance(statement, _NESTED_BLOCKS):
            yield from _module_level(_block_statements(statement))


def _imported_names(node: ast.Import | ast.ImportFrom) -> set[str]:
    return {(alias.asname or alias.name).split(".")[0] for alias in node.names}


def _assigned_names(node: ast.AST) -> set[str]:
    targets = [*getattr(node, "targets", []), getattr(node, "target", None)]
    walked = [name for target in filter(None, targets) for name in ast.walk(target)]
    return {name.id for name in walked if isinstance(name, ast.Name)}


def _bound_names(node: ast.AST) -> set[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return _imported_names(node)
    return _assigned_names(node)


@functools.cache
def _bindings(path: Path) -> frozenset[str] | None:
    """The names `path` binds at module level, or None when it resolves names on demand."""
    statements = list(_module_level(ast.parse(path.read_text(encoding="utf-8")).body))
    names = set().union(*(_bound_names(statement) for statement in statements))
    if "__getattr__" in names:
        return None
    return frozenset(names)


def _first_party_imports(path: Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node


def _provider_bindings(module: str) -> frozenset[str] | None:
    provider = _module_file(module)
    return _bindings(provider) if provider else None


def _is_bound(module: str, name: str, bound: frozenset[str]) -> bool:
    return name == "*" or name in bound or _module_file(f"{module}.{name}") is not None


def _unbound(node: ast.ImportFrom) -> list[str]:
    bound = _provider_bindings(node.module)
    if bound is None:
        return []
    return [alias.name for alias in node.names if not _is_bound(node.module, alias.name, bound)]


def _missing_imports(path: Path) -> list[str]:
    return [
        f"{path.relative_to(SCRIPTS)}:{node.lineno} {node.module}.{name}"
        for node in _first_party_imports(path) for name in _unbound(node)
    ]


def test_every_first_party_import_names_a_bound_name() -> None:
    missing = [entry for path in sorted(SCRIPTS.rglob("*.py")) for entry in _missing_imports(path)]
    assert missing == []


@pytest.mark.parametrize(
    ("provider", "names", "expected"),
    [
        ("x = 1\ndef y(): pass\n", ["x", "y"], []),
        ("x = 1\n", ["ROOT"], ["ROOT"]),
        ("try:\n    import json as ROOT\nexcept ImportError:\n    pass\n", ["ROOT"], []),
        ("def __getattr__(name): return name\n", ["ROOT"], []),
    ],
)
def test_the_check_sees_a_removed_name_and_only_that(tmp_path, monkeypatch, provider, names, expected) -> None:
    (tmp_path / "provider.py").write_text(provider, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "SCRIPTS", tmp_path)
    _bindings.cache_clear()
    node = ast.ImportFrom(module="provider", names=[ast.alias(name) for name in names], level=0)
    assert _unbound(node) == expected
