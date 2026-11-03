"""Conservative syntax checks for a directly imported public dependency API.

Not a Python sandbox or a whole-program proof. The qualification orchestrator
must check every relevant consumer source, keep other dependency source in full,
validate test patch sites separately and run real binding/integration checks.
Unsupported syntax keeps full source; it does not become an import exception.
"""
from __future__ import annotations

import ast

# Reflection and code loading cannot be resolved by this direct-import checker.
_DYNAMIC_NAMES = frozenset({'eval', 'exec', 'compile', 'globals', 'locals', 'vars',
                            'getattr', 'setattr', 'delattr', '__import__'})
_DYNAMIC_MODULES = frozenset({'importlib', 'inspect', 'sys', 'ctypes', 'builtins',
                              'pickle', 'marshal', 'types', 'runpy'})


def require_direct_public_access(source: str, module: str, exports: dict) -> dict:
    """Return local/public bindings only for supported explicit API accesses."""
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    bindings = _bindings(tree, module, exports)
    for node in ast.walk(tree):
        _check_node(node, parents, bindings, exports)
        _check_import_shadow(node, parents, bindings, module)
    return bindings


def _bindings(tree: ast.Module, module: str, exports: dict) -> dict:
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        _collect_binding(node, tree, module, exports, found)
    return found


def _collect_binding(node, tree, module: str, exports: dict, found: dict) -> None:
    if isinstance(node, ast.Import):
        _check_module_import(node, module)
        return
    if isinstance(node, ast.ImportFrom):
        _check_from_import(node, tree, module, exports, found)


def _check_module_import(node: ast.Import, module: str) -> None:
    names = {alias.name for alias in node.names}
    if any(_provider_spelling(name, module) for name in names):
        raise ValueError('Module-object dependency access is not qualified')
    _require_static_modules(names)


def _require_static_modules(names: set) -> None:
    roots = {name.split('.')[0] for name in names}
    if roots & _DYNAMIC_MODULES:
        raise ValueError('Unsupported reflective or dynamic import')


def _check_from_import(node, tree, module: str, exports: dict, found: dict) -> None:
    _require_static_modules({node.module or ''})
    _require_unambiguous_module(node, module)
    if node.module != module:
        return
    _require_top_level(node, tree)
    for alias in node.names:
        _add_binding(alias, exports, found)


def _require_top_level(node, tree) -> None:
    if node.level or node not in tree.body:
        raise ValueError('Only top-level absolute dependency imports are qualified')


def _require_unambiguous_module(node: ast.ImportFrom, module: str) -> None:
    if node.module == module:
        return
    if any(_provider_spelling(name, module) for name in _import_spellings(node)):
        raise ValueError('Alternate dependency module spellings are not qualified')


def _import_spellings(node) -> set:
    return {node.module or '', *(alias.name for alias in node.names)}


def _provider_spelling(name: str, module: str) -> bool:
    return name == module or name.endswith('.' + module) or name.startswith(module + '.')


def _add_binding(alias: ast.alias, exports: dict, found: dict) -> None:
    if alias.name.startswith('_') or alias.name not in exports:
        raise ValueError(f'Private or undeclared dependency member: {alias.name}')
    local = alias.asname or alias.name
    if local in found:
        raise ValueError(f'Duplicate dependency binding: {local}')
    found[local] = alias.name


def _check_node(node, parents: dict, bindings: dict, exports: dict) -> None:
    if isinstance(node, ast.Name):
        _check_name(node, parents, bindings, exports)
    if isinstance(node, ast.Attribute):
        _require_static_attribute(node)
    _check_shadow(node, bindings)


def _require_static_attribute(node: ast.Attribute) -> None:
    if node.attr.startswith('__') or node.attr in _DYNAMIC_NAMES:
        raise ValueError('Reflective attribute access is not qualified')


def _check_import_shadow(node, parents: dict, bindings: dict, module: str) -> None:
    if not isinstance(node, ast.alias):
        return
    local = node.asname or node.name.split('.')[0]
    if local in bindings and getattr(parents[node], 'module', None) != module:
        raise ValueError(f'Another import shadows the dependency: {local}')


def _check_name(node, parents: dict, bindings: dict, exports: dict) -> None:
    if node.id in _DYNAMIC_NAMES or node.id.startswith('__'):
        raise ValueError(f'Dynamic dispatch is not qualified: {node.id}')
    if node.id not in bindings:
        return
    _require_read(node)
    _require_value_use(node, parents, exports[bindings[node.id]])


def _require_read(node: ast.Name) -> None:
    if not isinstance(node.ctx, ast.Load):
        raise ValueError(f'Dependency binding is reassigned: {node.id}')


def _require_value_use(node, parents: dict, kind: str) -> None:
    if kind == 'constant':
        return
    parent = parents[node]
    if kind != 'function' or not isinstance(parent, ast.Call) or parent.func is not node:
        raise ValueError(f'Escaping or unsupported dependency value: {node.id}')


def _check_shadow(node, bindings: dict) -> None:
    names = _declared_names(node)
    if names & bindings.keys():
        raise ValueError(f'Dependency binding is shadowed: {sorted(names & bindings.keys())}')


def _declared_names(node) -> set:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.ExceptHandler)):
        return {node.name}
    if isinstance(node, ast.arg):
        return {node.arg}
    return _other_bound_names(node)


def _other_bound_names(node) -> set:
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return set(node.names)
    if isinstance(node, (ast.MatchAs, ast.MatchStar)):
        return {node.name}
    return set()
