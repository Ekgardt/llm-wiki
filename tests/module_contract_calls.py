"""Bind statically known consumer call shapes to the checked public interface."""
from __future__ import annotations

import ast
import inspect

from tests.module_contract_access import require_direct_public_access
from tests.module_contract_surface import declared_kinds


def require_public_calls(source: str, module: str, interface: str) -> dict:
    kinds = declared_kinds(interface)
    bindings = require_direct_public_access(source, module, kinds)
    _require_tree_call_shapes(ast.parse(source), bindings, interface)
    return bindings


def _require_tree_call_shapes(tree, bindings: dict, interface: str) -> None:
    signatures = _signatures(interface)
    for node in ast.walk(tree):
        _check_call(node, bindings, signatures)


def _signatures(interface: str) -> dict:
    functions = [node for node in ast.parse(interface).body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return {node.name: _signature(node.args) for node in functions}


def _signature(arguments: ast.arguments) -> inspect.Signature:
    parameters = _positional_parameters(arguments)
    parameters.extend(_variadic(arguments.vararg, inspect.Parameter.VAR_POSITIONAL))
    parameters.extend(_keyword_parameters(arguments))
    parameters.extend(_variadic(arguments.kwarg, inspect.Parameter.VAR_KEYWORD))
    return inspect.Signature(parameters)


def _positional_parameters(arguments: ast.arguments) -> list:
    all_positional = arguments.posonlyargs + arguments.args
    required = len(all_positional) - len(arguments.defaults)
    return [_positional_parameter(arg, index, len(arguments.posonlyargs), required)
            for index, arg in enumerate(all_positional)]


def _positional_parameter(arg, index: int, positional_only: int, required: int):
    kind = inspect.Parameter.POSITIONAL_OR_KEYWORD
    if index < positional_only:
        kind = inspect.Parameter.POSITIONAL_ONLY
    return inspect.Parameter(arg.arg, kind, default=_default(index >= required))


def _default(present: bool):
    if present:
        return None  # Only requiredness matters; no default expression is executed.
    return inspect.Parameter.empty


def _keyword_parameters(arguments: ast.arguments) -> list:
    return [inspect.Parameter(arg.arg, inspect.Parameter.KEYWORD_ONLY,
                              default=_default(value is not None))
            for arg, value in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True)]


def _variadic(arg, kind) -> list:
    if arg is None:
        return []
    return [inspect.Parameter(arg.arg, kind)]


def _check_call(node, bindings: dict, signatures: dict) -> None:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return
    public = bindings.get(node.func.id)
    if public is not None:
        _require_callable(node, public, signatures)


def _require_callable(node: ast.Call, public: str, signatures: dict) -> None:
    if public not in signatures:
        raise ValueError(f'Public constant is not a declared callable: {public}')
    _bind_call(node, signatures[public])


def _bind_call(node: ast.Call, signature: inspect.Signature) -> None:
    _require_known_arguments(node)
    keywords = {kw.arg: None for kw in node.keywords}
    try:
        signature.bind(*([None] * len(node.args)), **keywords)
    except TypeError as exc:
        raise ValueError(f'Consumer call does not match public interface: {exc}') from exc


def _require_known_arguments(node: ast.Call) -> None:
    if any(isinstance(arg, ast.Starred) for arg in node.args):
        raise ValueError('Dynamic positional expansion requires full source')
    _require_known_keywords([kw.arg for kw in node.keywords])


def _require_known_keywords(names: list) -> None:
    if None in names or len(set(names)) != len(names):
        raise ValueError('Dynamic or duplicate keyword arguments require full source')
