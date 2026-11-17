"""A test that patches a facade patches a name some code reads through it.

A split leaves a public module that re-exports names its concern modules own.
The concern modules read their own globals, so a stand-in set on the facade's
copy changes nothing unless the facade itself, an attribute read
`facade.name`, or a call-time `from facade import name` reaches it. Such a
test keeps passing while it no longer tests what it says; this guard names it.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
PATCH_CALL = r"(?:setattr|patch\.object)\(\s*{alias}\s*,\s*[\"'](\w+)"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _bound_names(node: ast.ImportFrom) -> set[str]:
    return {alias.asname or alias.name for alias in node.names}


def _is_concern_import(node: ast.AST) -> bool:
    return isinstance(node, ast.ImportFrom) and (node.module or "").startswith("llm_wiki")


def _reexported(tree: ast.Module) -> set[str]:
    return set().union(*map(_bound_names, filter(_is_concern_import, tree.body)))


def _is_load(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)


def _loaded_names(tree: ast.AST) -> set[str]:
    return {node.id for node in ast.walk(tree) if _is_load(node)}


def _is_function(node: ast.AST) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))


def _imported_pairs(node: ast.AST) -> Iterator[tuple[str, str]]:
    if isinstance(node, ast.ImportFrom) and node.module:
        yield from ((node.module, alias.name) for alias in node.names)


def _call_time_imports(tree: ast.Module) -> Iterator[tuple[str, str]]:
    for function in filter(_is_function, ast.walk(tree)):
        for node in ast.walk(function):
            yield from _imported_pairs(node)


def _attribute_reads(tree: ast.Module) -> Iterator[tuple[str, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            yield node.value.id, node.attr


def _reached_through_modules(sources: dict[Path, str]) -> set[tuple[str, str]]:
    """Every (module, name) pair some script reads at call time through the module."""
    reached: set[tuple[str, str]] = set()
    for text in sources.values():
        tree = ast.parse(text)
        reached.update(_attribute_reads(tree), _call_time_imports(tree))
    return reached


def _unreached_reexports(facade_path: Path, reached: set[tuple[str, str]]) -> set[str]:
    tree = _tree(facade_path)
    names = _reexported(tree) - _loaded_names(tree)
    return {name for name in names if (facade_path.stem, name) not in reached}


def _patched_names(text: str, facade: str) -> Iterator[tuple[int, str]]:
    aliases = {facade, *re.findall(rf"import {facade} as (\w+)", text)}
    patterns = [re.compile(rf"""["']{facade}\.(\w+)["']""")]
    patterns += [re.compile(PATCH_CALL.format(alias=alias)) for alias in aliases]
    for pattern in patterns:
        for match in pattern.finditer(text):
            yield text.count("\n", 0, match.start()) + 1, match.group(1)


def _facades() -> list[Path]:
    return [path for path in sorted(SCRIPTS.glob("*.py")) if _reexported(_tree(path))]


def _read_all(paths: Iterator[Path]) -> dict[Path, str]:
    return {path: path.read_text(encoding="utf-8") for path in sorted(paths)}


def _dead_patches_of(facade_path: Path, reached: set[tuple[str, str]], tests: dict[Path, str]) -> Iterator[str]:
    unreached = _unreached_reexports(facade_path, reached)
    for path, text in tests.items():
        dead = (pair for pair in _patched_names(text, facade_path.stem) if pair[1] in unreached)
        yield from (f"{path.relative_to(ROOT)}:{line}: {facade_path.stem}.{name}" for line, name in dead)


def _other_tests() -> dict[Path, str]:
    return _read_all(path for path in TESTS.rglob("*.py") if path != Path(__file__).resolve())


def _dead_patches() -> list[str]:
    reached = _reached_through_modules(_read_all(SCRIPTS.rglob("*.py")))
    tests = _other_tests()
    return [finding for facade_path in _facades() for finding in _dead_patches_of(facade_path, reached, tests)]


def test_no_test_patches_a_name_its_facade_only_reexports() -> None:
    assert _dead_patches() == []


def test_the_guard_names_a_patch_on_an_unread_reexport() -> None:
    text = 'monkeypatch.setattr(flush_memory, "classify_response", lambda raw: ("major", raw))\n'
    assert list(_patched_names(text, "flush_memory")) == [(1, "classify_response")]
    reached = _reached_through_modules(_read_all(SCRIPTS.rglob("*.py")))
    assert "classify_response" in _unreached_reexports(SCRIPTS / "flush_memory.py", reached)
