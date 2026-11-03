"""Compare explicit public declarations without executing interface stubs.

This checks locally declared functions/constants, not runtime binding or semantic
equivalence. Imported helpers are not published API. Unsupported public shapes
must retain full implementation context until a checker for them is qualified.
"""
from __future__ import annotations

import ast
import copy


def declared_surface(source: str, *, interface: bool = False) -> dict[str, str]:
    result: dict[str, str] = {}
    for node in ast.parse(source).body:
        _add_declaration(result, node, interface=interface)
    if not result:
        raise ValueError('No explicit public declarations')
    return result


def _add_declaration(result: dict, node: ast.AST, *, interface: bool) -> None:
    entry = _declaration(node, interface=interface)
    if entry is None:
        return
    name, value = entry
    if name in result:
        raise ValueError(f'Duplicate public declaration: {name}')
    result[name] = value


def _declaration(node: ast.AST, *, interface: bool):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _function(node, interface=interface)
    if isinstance(node, ast.Assign):
        return _assignment(node)
    return _non_api_statement(node, interface=interface)


def _function(node, *, interface: bool):
    if node.name.startswith('_'):
        return None
    _require_plain_function(node, interface=interface)
    header = copy.copy(node)
    header.body = [ast.Expr(value=ast.Constant(value=Ellipsis))]
    return node.name, ast.dump(header)


def _require_plain_function(node, *, interface: bool) -> None:
    if node.decorator_list:
        raise ValueError('Decorated public functions need a qualified runtime contract')
    if interface and not _stub_body(node.body):
        raise ValueError('Interface functions must contain only an ellipsis')


def _stub_body(body: list) -> bool:
    expected = ast.parse('...').body
    return [ast.dump(node) for node in body] == [ast.dump(node) for node in expected]


def _assignment(node: ast.Assign):
    if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
        raise ValueError('Only simple named constant declarations are supported')
    name = node.targets[0].id
    if name.startswith('_'):
        return None
    return name, ast.dump(node)


def _non_api_statement(node: ast.AST, *, interface: bool):
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return _import_statement(node, interface=interface)
    if _docstring_or_private_class(node):
        return None
    raise ValueError(f'Unsupported top-level declaration: {type(node).__name__}')


def _import_statement(node, *, interface: bool):
    if any(alias.name == '*' for alias in node.names):
        raise ValueError('Star imports cannot establish a complete public surface')
    if interface and getattr(node, 'module', None) != '__future__':
        raise ValueError('This interface grammar only permits future imports')
    return None


def _docstring_or_private_class(node: ast.AST) -> bool:
    if isinstance(node, ast.ClassDef):
        return node.name.startswith('_')
    if isinstance(node, ast.Expr):
        return isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    return False


def require_declared_surface(provider: str, interface: str) -> set[str]:
    """Full explicit declarations must agree; body behavior is checked elsewhere."""
    actual = declared_surface(provider)
    expected = declared_surface(interface, interface=True)
    if actual != expected:
        raise ValueError('Public interface changed; requalification required')
    return set(actual)


def declared_kinds(interface: str) -> dict[str, str]:
    """Derive member kinds from the validated interface, not a caller allowlist."""
    names = declared_surface(interface, interface=True)
    functions = {node.name for node in ast.parse(interface).body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    result = dict.fromkeys(names, 'constant')
    result.update(dict.fromkeys(functions & names.keys(), 'function'))
    return result
