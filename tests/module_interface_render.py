"""Render the exact, derived description of the provider names a consumer reaches."""
from __future__ import annotations

import ast
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from tests.module_interface import imported_module, provider_path

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
CALLABLE_DUNDERS = frozenset({'__init__', '__call__', '__enter__', '__exit__', '__iter__', '__post_init__'})
EFFECT_METHODS = {
    'write_text': 'filesystem', 'write_bytes': 'filesystem', 'unlink': 'filesystem', 'mkdir': 'filesystem',
    'touch': 'filesystem', 'rename': 'filesystem', 'rmdir': 'filesystem', 'open': 'filesystem',
    'execute': 'database', 'executemany': 'database', 'executescript': 'database', 'commit': 'database',
    'kill': 'process', 'terminate': 'process', 'sleep': 'blocks',
}
EFFECT_OWNERS = {'os': 'filesystem', 'shutil': 'filesystem', 'subprocess': 'process', 'socket': 'network', 'urllib': 'network'}


@dataclass(frozen=True)
class Provider:
    path: Path
    text: str
    definitions: dict[str, ast.AST]
    imported: dict[str, tuple[str, str]]
    root: Path


@dataclass
class _Facts:
    raises: set[str] = field(default_factory=set)
    effects: set[str] = field(default_factory=set)
    delegates: set[str] = field(default_factory=set)
    state: set[str] = field(default_factory=set)


def _conditional_blocks(node: ast.stmt) -> list[list[ast.stmt]]:
    if not isinstance(node, (ast.If, ast.Try)):
        return []
    return [getattr(node, block, []) for block in ('body', 'orelse', 'finalbody')]


def _top_level(body: list[ast.stmt]) -> Iterable[ast.stmt]:
    for node in body:
        yield node
        for block in _conditional_blocks(node):
            yield from _top_level(block)


def _assigned_names(node: ast.stmt) -> list[str]:
    targets = getattr(node, 'targets', [getattr(node, 'target', None)])
    return [t.id for t in targets if isinstance(t, ast.Name)]


def _defined_names(node: ast.stmt) -> list[str]:
    if isinstance(node, (*FUNCTIONS, ast.ClassDef)):
        return [node.name]
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        return _assigned_names(node)
    return []


def _imported_pairs(root: Path, path: Path, node: ast.stmt) -> list[tuple[str, tuple[str, str]]]:
    if isinstance(node, ast.ImportFrom):
        return _from_import_pairs(imported_module(root, path, node), node)
    if isinstance(node, ast.Import):
        return _module_import_pairs(node)
    return []


def _from_import_pairs(module: str, node: ast.ImportFrom) -> list[tuple[str, tuple[str, str]]]:
    return [(alias.asname or alias.name, (module, alias.name)) for alias in node.names]


def _module_import_pairs(node: ast.Import) -> list[tuple[str, tuple[str, str]]]:
    return [(alias.asname or alias.name, (alias.name, '')) for alias in node.names]


@cache
def _parsed(path: Path, root: Path, digest: bytes) -> Provider:
    text = path.read_bytes().decode('utf-8')
    statements = list(_top_level(ast.parse(text).body))
    definitions = {name: node for node in statements for name in _defined_names(node)}
    imported = dict(pair for node in statements for pair in _imported_pairs(root, path, node))
    return Provider(path, text, definitions, imported, root)


def load_provider(root: Path, path: Path) -> Provider:
    return _parsed(path, root, hashlib.sha256(path.read_bytes()).digest())


def _exception_name(node: ast.Raise) -> str:
    target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
    return getattr(target, 'attr', getattr(target, 'id', ''))


def _call_owner(function: ast.expr) -> str:
    return getattr(getattr(function, 'value', None), 'id', '')


def _note_effect(function: ast.expr, facts: _Facts) -> None:
    method = getattr(function, 'attr', getattr(function, 'id', ''))
    owner = _call_owner(function)
    effect = EFFECT_OWNERS.get(owner) or EFFECT_METHODS.get(method)
    if effect:
        facts.effects.add(effect)


def _note_delegate(provider: Provider, function: ast.expr, facts: _Facts) -> None:
    local = getattr(function, 'id', _call_owner(function))
    module = provider.imported.get(local, ('', ''))[0]
    if module and provider_path(provider.root, module) is not None:
        facts.delegates.add(module)


def _callee(provider: Provider, function: ast.expr, methods: dict[str, ast.AST]) -> ast.AST | None:
    if isinstance(function, ast.Name):
        return provider.definitions.get(function.id)
    return methods.get(function.attr) if _call_owner(function) == 'self' else None


def _scan_call(provider: Provider, node: ast.Call, methods: dict, facts: _Facts, pending: list) -> None:
    _note_effect(node.func, facts)
    _note_delegate(provider, node.func, facts)
    callee = _callee(provider, node.func, methods) if isinstance(node.func, (ast.Name, ast.Attribute)) else None
    if isinstance(callee, FUNCTIONS):
        pending.append(callee)


def _note_statement_facts(node: ast.AST, facts: _Facts) -> None:
    if isinstance(node, ast.Raise) and node.exc is not None:
        facts.raises.add(_exception_name(node))
    if isinstance(node, ast.Global):
        facts.state.update(node.names)


def _scan_node(provider: Provider, node: ast.AST, methods: dict, facts: _Facts, pending: list) -> None:
    if isinstance(node, ast.Call):
        _scan_call(provider, node, methods, facts, pending)
        return
    _note_statement_facts(node, facts)


def _facts(provider: Provider, function: ast.AST, methods: dict[str, ast.AST]) -> _Facts:
    facts, pending, seen = _Facts(), [function], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        for node in ast.walk(current):
            _scan_node(provider, node, methods, facts, pending)
    return facts


def _fact_lines(facts: _Facts, indent: str) -> list[str]:
    labels = (('raises', facts.raises), ('effects', facts.effects), ('delegates', facts.delegates), ('state', facts.state))
    return [f'{indent}# {label}: {", ".join(sorted(v for v in values if v))}' for label, values in labels if any(values)]


def _decorators(node: ast.AST, indent: str) -> list[str]:
    return [f'{indent}@{ast.unparse(item)}' for item in getattr(node, 'decorator_list', ())]


def _signature(node: ast.AST) -> str:
    prefix = 'async def' if isinstance(node, ast.AsyncFunctionDef) else 'def'
    returns = f' -> {ast.unparse(node.returns)}' if node.returns else ''
    return f'{prefix} {node.name}({ast.unparse(node.args)}){returns}:'


def _docstring_lines(node: ast.AST, indent: str) -> list[str]:
    text = ast.get_docstring(node)
    return [f'{indent}"""{text}"""'] if text else []


def _function_record(provider: Provider, node: ast.AST, methods: dict, indent: str) -> str:
    inner = indent + '    '
    lines = [*_decorators(node, indent), indent + _signature(node), *_docstring_lines(node, inner)]
    lines.extend(_fact_lines(_facts(provider, node, methods), inner))
    return '\n'.join([*lines, inner + '...'])


def _exact_source(provider: Provider, node: ast.AST, indent: str) -> str:
    segment = ast.get_source_segment(provider.text, node, padded=False) or ''
    decorators = _decorators(node, indent)
    return '\n'.join([*decorators, indent + segment])


def describe_function(provider: Provider, node: ast.AST, methods: dict, indent: str = '') -> str:
    """The source itself whenever it is no longer than its description."""
    record = _function_record(provider, node, methods, indent)
    source = _exact_source(provider, node, indent)
    return source if len(source) <= len(record) else record


def _public_member(node: ast.AST) -> bool:
    return node.name in CALLABLE_DUNDERS or not node.name.startswith('_')


def _class_methods(node: ast.ClassDef) -> dict[str, ast.AST]:
    return {item.name: item for item in node.body if isinstance(item, FUNCTIONS)}


def _class_fields(provider: Provider, node: ast.ClassDef) -> list[str]:
    fields = [item for item in node.body if isinstance(item, (ast.Assign, ast.AnnAssign))]
    return ['    ' + (ast.get_source_segment(provider.text, item) or '') for item in fields]


def _class_header(node: ast.ClassDef) -> str:
    bases = ', '.join(ast.unparse(item) for item in [*node.bases, *node.keywords])
    return f'class {node.name}({bases}):' if bases else f'class {node.name}:'


def describe_class(provider: Provider, node: ast.ClassDef) -> str:
    methods = _class_methods(node)
    public = [m for m in methods.values() if _public_member(m)]
    rendered = [describe_function(provider, method, methods, '    ') for method in public]
    head = [*_decorators(node, ''), _class_header(node), *_docstring_lines(node, '    ')]
    return '\n'.join([*head, *_class_fields(provider, node), *rendered, '    ...'])


def describe_definition(provider: Provider, node: ast.AST) -> str:
    if isinstance(node, FUNCTIONS):
        return describe_function(provider, node, {})
    if isinstance(node, ast.ClassDef):
        return describe_class(provider, node)
    return ast.get_source_segment(provider.text, node) or ''


def _annotation_names(node: ast.AST) -> set[str]:
    annotations = _annotations_of(node)
    return {item.id for a in annotations for item in ast.walk(a) if isinstance(item, ast.Name)}


def _annotations_of(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, FUNCTIONS):
        return _function_annotations(node)
    if isinstance(node, ast.ClassDef):
        return [*node.bases, *(item.annotation for item in node.body if isinstance(item, ast.AnnAssign))]
    return []


def _function_annotations(node: ast.AST) -> list[ast.AST]:
    arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
    annotations = [item.annotation for item in arguments] + [node.returns]
    return [item for item in annotations if item is not None]


def _related_names(provider: Provider, node: ast.AST) -> set[str]:
    return {name for name in _annotation_names(node) if isinstance(provider.definitions.get(name), ast.ClassDef)}


def _reexport_origin(provider: Provider, name: str) -> tuple[Provider, str] | None:
    module, original = provider.imported.get(name, ('', ''))
    if not (module and original):
        return None
    origin = provider_path(provider.root, module)
    if origin is None:
        return None
    return load_provider(provider.root, origin), original


def resolve_definition(provider: Provider, name: str) -> tuple[Provider, str] | None:
    """Follow re-exports to the defining file; None means the name has no description."""
    seen = set()
    while name not in provider.definitions and (provider.path, name) not in seen:
        seen.add((provider.path, name))
        origin = _reexport_origin(provider, name)
        if origin is None:
            return None
        provider, name = origin
    return (provider, name) if name in provider.definitions else None


def _closed_names(provider: Provider, names: set[str]) -> set[str]:
    pending, closed = list(names), set()
    while pending:
        name = pending.pop()
        if name in closed:
            continue
        closed.add(name)
        node = provider.definitions.get(name)
        pending.extend(_related_names(provider, node) if node is not None else ())
    return closed


def _described(provider: Provider, names: set[str]) -> list[str]:
    nodes = {name: provider.definitions[name] for name in sorted(_closed_names(provider, names)) if name in provider.definitions}
    unique = {id(node): node for node in nodes.values()}
    return [describe_definition(provider, node) for node in unique.values()]


def provider_contract(root: Path, path: Path, names: set[str]) -> tuple[str, set[str]]:
    """Return the derived text and the names that cannot be described from this file."""
    provider = load_provider(root, path)
    origins: dict[Path, set[str]] = {}
    unknown: set[str] = set()
    for name in sorted(names):
        _place_name(provider, name, origins, unknown)
    _place_inherited(root, origins)
    return _contract_sections(root, origins), unknown


def _base_names(node: ast.AST | None) -> list[str]:
    return [item.id for item in getattr(node, 'bases', ()) if isinstance(item, ast.Name)]


def _resolved_bases(root: Path, path: Path, name: str) -> list[tuple[Path, str]]:
    provider = load_provider(root, path)
    resolved = (resolve_definition(provider, base) for base in _base_names(provider.definitions.get(name)))
    return [(item[0].path, item[1]) for item in resolved if item is not None]


def _new_bases(root: Path, path: Path, name: str, origins: dict[Path, set[str]]) -> list[tuple[Path, str]]:
    found = _resolved_bases(root, path, name)
    return [item for item in found if item[1] not in origins.get(item[0], set())]


def _place_inherited(root: Path, origins: dict[Path, set[str]]) -> None:
    """A class composed from bases in other files is described with what it inherits."""
    pending = [(path, name) for path, names in origins.items() for name in names]
    while pending:
        path, name = pending.pop()
        for base_path, base in _new_bases(root, path, name, origins):
            origins.setdefault(base_path, set()).add(base)
            pending.append((base_path, base))


def _place_name(provider: Provider, name: str, origins: dict[Path, set[str]], unknown: set[str]) -> None:
    resolved = resolve_definition(provider, name)
    if resolved is not None:
        origins.setdefault(resolved[0].path, set()).add(resolved[1])
        return
    if name not in provider.imported:
        unknown.add(name)



def _contract_sections(root: Path, origins: dict[Path, set[str]]) -> str:
    sections = []
    for path in sorted(origins):
        body = '\n\n'.join(_described(load_provider(root, path), origins[path]))
        sections.append(f'# from {path.relative_to(root).as_posix()}\n{body}\n')
    return '\n'.join(sections)


def _called_definitions(provider: Provider, node: ast.AST, found: set[str]) -> list[ast.AST]:
    called = {item.id for item in ast.walk(node) if isinstance(item, ast.Name) and item.id in provider.definitions}
    found.update(called)
    return [provider.definitions[name] for name in called]


def _reached_definitions(provider: Provider, names: set[str]) -> set[str]:
    """Top-level names a public call can reach inside the provider."""
    pending = [provider.definitions[name] for name in names if name in provider.definitions]
    found: set[str] = set()
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) not in seen:
            seen.add(id(current))
            pending.extend(_called_definitions(provider, current, found))
    return found


def _mentioned(node: ast.AST) -> set[str]:
    return {item.id if isinstance(item, ast.Name) else item.attr
            for item in ast.walk(node) if isinstance(item, (ast.Name, ast.Attribute))}


def names_run_by(root: Path, path: Path, names: frozenset[str]) -> frozenset[str]:
    """The names a call to `names` can run, itself included: what it may borrow from elsewhere."""
    provider = load_provider(root, path)
    reached = names | _reached_definitions(provider, set(names))
    bodies = [provider.definitions[name] for name in reached if name in provider.definitions]
    return frozenset(reached.union(*map(_mentioned, bodies)))


def _patch_is_a_public_callable(provider: Provider, name: str) -> bool:
    """A public name bound to a function or class, here or where a re-export took it from."""
    origin = resolve_definition(provider, name)
    if origin is None or name.startswith('_'):
        return False
    owner, original = origin
    return isinstance(owner.definitions[original], (*FUNCTIONS, ast.ClassDef))


def patches_depend_on_internals(root: Path, path: Path, patched: set[str], accessed: set[str]) -> bool:
    """A stand-in is safe only for a public callable the test does not also reach through."""
    provider = load_provider(root, path)
    if not all(_patch_is_a_public_callable(provider, name) for name in patched):
        return True
    reached = accessed - patched
    if _reached_definitions(provider, reached) & patched:
        return True
    borrowed = patched - provider.definitions.keys()
    return bool(names_run_by(root, path, frozenset(reached)) & borrowed)
