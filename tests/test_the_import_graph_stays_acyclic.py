"""What the module split bought must not quietly come back.

Scripts import each other, including from inside functions, and a split module is
only independently maintainable while those imports form no cycle. A name a module
rebinds with ``global`` must also stay read through its owner: ``from owner import
name`` copies the value at import time and answers with it forever.
"""
from __future__ import annotations

import ast
import graphlib
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(SCRIPTS).with_suffix("").parts).removesuffix(".__init__")


def _script_trees() -> dict[str, ast.Module]:
    return {_module_name(path): ast.parse(path.read_text(encoding="utf-8")) for path in SCRIPTS.rglob("*.py")}


def _modules_of_from_import(node: ast.ImportFrom) -> list[str]:
    if node.level or not node.module:
        return []
    return [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]


def _named_modules(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return _modules_of_from_import(node)
    return []


def _imported_by(tree: ast.Module) -> set[str]:
    return {target for node in ast.walk(tree) for target in _named_modules(node)}


def _import_graph(trees: dict[str, ast.Module]) -> dict[str, set[str]]:
    return {name: _imported_by(tree) & (set(trees) - {name}) for name, tree in trees.items()}


def _import_cycle(graph: dict[str, set[str]]) -> list[str]:
    try:
        graphlib.TopologicalSorter(graph).prepare()
    except graphlib.CycleError as error:
        return list(error.args[1])
    return []


def _rebound_globals(tree: ast.Module) -> set[str]:
    return {name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names}


def _from_imports(tree: ast.Module) -> list[tuple[str | None, str]]:
    return [
        (node.module, alias.name)
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]


def _snapshots_of_rebound_globals(trees: dict[str, ast.Module]) -> list[str]:
    rebound = {name: _rebound_globals(tree) for name, tree in trees.items()}
    return sorted(
        f"{importer} copies {module}.{name}"
        for importer, tree in trees.items()
        for module, name in _from_imports(tree)
        if name in rebound.get(module, ())
    )


def _trees(**sources: str) -> dict[str, ast.Module]:
    return {name: ast.parse(source) for name, source in sources.items()}


def test_the_scripts_import_each_other_without_a_cycle() -> None:
    assert _import_cycle(_import_graph(_script_trees())) == []


def test_a_cycle_through_a_function_local_import_is_found() -> None:
    planted = _trees(first="import second\n", second="def late():\n    import first\n")

    assert set(_import_cycle(_import_graph(planted))) == {"first", "second"}


def test_no_script_copies_a_global_its_owner_rebinds() -> None:
    assert _snapshots_of_rebound_globals(_script_trees()) == []


def test_a_copy_of_a_rebound_global_is_found() -> None:
    planted = _trees(
        owner="reason = None\ndef note():\n    global reason\n    reason = 'gone'\n",
        reader="from owner import reason\n",
    )

    assert _snapshots_of_rebound_globals(planted) == ["reader copies owner.reason"]
