"""Recognize public consumer-side fault injection without hiding test expressions.

This syntactic allowance is not provider qualification. The runner must verify
the actual pytest fixture, restored bindings, and independent unmocked behavior.
No manifest-supplied exemption list is accepted.
"""
from __future__ import annotations

import ast

from tests.module_contract_access import _declared_names, _parent_map, _require_access_tree
from tests.module_contract_calls import _require_tree_call_shapes, require_public_calls
from tests.module_contract_surface import declared_kinds


def require_test_public_calls(source: str, *, module: str, interface: str,
                              consumer_module: str, consumer_source: str) -> dict:
    consumer_bindings = require_public_calls(consumer_source, module, interface)
    kinds = declared_kinds(interface)
    tree = ast.parse(source)
    approved = _public_patch_attributes(tree, consumer_module, consumer_bindings, kinds)
    bindings = _require_access_tree(tree, module, kinds, approved_attributes=approved)
    _require_tree_call_shapes(tree, bindings, interface)
    return bindings


def _public_patch_attributes(tree, consumer: str, bindings: dict, kinds: dict) -> set:
    imports = _consumer_imports(tree, consumer)
    aliases = set(imports.values())
    _require_stable_aliases(tree, imports, aliases)
    _require_no_reexports(tree, consumer, aliases, bindings)
    parents = _parent_map(tree)
    calls = [node for node in ast.walk(tree) if _is_setattr_call(node)]
    for call in calls:
        _require_public_patch(call, parents, aliases, bindings, kinds)
    return {call.func for call in calls}


def _require_no_reexports(tree, consumer: str, aliases: set, bindings: dict) -> None:
    for node in ast.walk(tree):
        _require_no_reexport_import(node, consumer, bindings)
        _require_no_reexport_attribute(node, aliases, bindings)


def _require_no_reexport_import(node, consumer: str, bindings: dict) -> None:
    if not isinstance(node, ast.ImportFrom) or node.module != consumer:
        return
    if any(alias.name in bindings for alias in node.names):
        raise ValueError('Indirect dependency reexports require full source')


def _require_no_reexport_attribute(node, aliases: set, bindings: dict) -> None:
    if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
        return
    if node.value.id in aliases and node.attr in bindings:
        raise ValueError('Indirect dependency reexports require full source')


def _consumer_imports(tree, consumer: str) -> dict:
    found = {}
    for node in tree.body:
        found.update(_imported_consumer_names(node, consumer))
    return found


def _imported_consumer_names(node, consumer: str) -> dict:
    if not isinstance(node, ast.Import):
        return {}
    return {alias: alias.asname or alias.name for alias in node.names if alias.name == consumer}


def _is_setattr_call(node) -> bool:
    if not isinstance(node, ast.Call):
        return False
    return isinstance(node.func, ast.Attribute) and node.func.attr == 'setattr'


def _require_stable_aliases(tree, imports: dict, aliases: set) -> None:
    protected = aliases | {'monkeypatch'}
    for node in ast.walk(tree):
        _require_no_write(node, protected)
        _require_no_shadow(node, imports, aliases, protected)


def _require_no_write(node, names: set) -> None:
    if not isinstance(node, ast.Name) or isinstance(node.ctx, ast.Load):
        return
    if node.id in names:
        raise ValueError('Test rebinds its patch target or fixture')


def _require_no_shadow(node, imports: dict, aliases: set, protected: set) -> None:
    if isinstance(node, ast.arg) and node.arg == 'monkeypatch':
        return  # The nearest test function must request this exact pytest fixture.
    if _declared_names(node) & protected:
        raise ValueError('Test shadows its patch target or fixture')
    _require_no_import_shadow(node, imports, aliases | {'monkeypatch'})


def _require_no_import_shadow(node, imports: dict, protected: set) -> None:
    if not isinstance(node, ast.alias) or node in imports:
        return
    if (node.asname or node.name.split('.')[0]) in protected:
        raise ValueError('Test imports over its patch target or fixture')


def _require_public_patch(call, parents: dict, aliases: set, bindings: dict, kinds: dict) -> None:
    _require_fixture_call(call, parents)
    _require_patch_arguments(call)
    target, name, _replacement = call.args
    if not isinstance(target, ast.Name) or target.id not in aliases:
        raise ValueError('Patch must target the explicitly imported consumer module')
    _require_public_function(name, bindings, kinds)


def _require_fixture_call(call, parents: dict) -> None:
    owner = call.func.value
    if not isinstance(owner, ast.Name) or owner.id != 'monkeypatch':
        raise ValueError('Only the explicit pytest monkeypatch fixture is supported')
    function = _enclosing_function(call, parents)
    _require_test_fixture(function)


def _enclosing_function(node, parents: dict):
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = parents.get(current)
    raise ValueError('Public fault injection must be inside a test function')


def _require_test_fixture(function) -> None:
    arguments = function.args.posonlyargs + function.args.args + function.args.kwonlyargs
    names = {arg.arg for arg in arguments}
    if not function.name.startswith('test_') or 'monkeypatch' not in names:
        raise ValueError('Public fault injection must request the pytest fixture')


def _require_patch_arguments(call) -> None:
    if len(call.args) != 3 or call.keywords:
        raise ValueError('Only explicit object/name/replacement patches are supported')


def _require_public_function(name, bindings: dict, kinds: dict) -> None:
    if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
        raise ValueError('Patch member name must be a literal string')
    public = bindings.get(name.value)
    if kinds.get(public) != 'function':
        raise ValueError('Patch crosses a private, undeclared or non-function boundary')
