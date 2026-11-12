"""Find which provider names a source file reaches, from its real syntax tree."""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from tests.module_context_paths import path_loaded_sources, syntax_nodes

PATCH_CALLS = frozenset({'patch', 'setattr', 'delattr', 'object'})
IMPORT_CALLS = frozenset({'setitem', 'import_module', '__import__'})
READ_CALLS = frozenset({'getattr', 'hasattr'})
PROVIDER_ROOTS = ('scripts', 'benchmark')
PACKAGE_ROOT = 'scripts'


@dataclass
class SourceAccesses:
    """Names reached through the public door, and providers reached around it."""

    accessed: dict[Path, set[str]] = field(default_factory=dict)
    patched: dict[Path, set[str]] = field(default_factory=dict)
    opaque: set[Path] = field(default_factory=set)

    def reach(self, provider: Path, name: str) -> None:
        self.accessed.setdefault(provider, set()).add(name)
        if _is_private(name):
            self.opaque.add(provider)

    def patch(self, provider: Path, name: str) -> None:
        self.patched.setdefault(provider, set()).add(name)
        self.reach(provider, name)

    def merge(self, other: SourceAccesses) -> None:
        for provider, names in other.accessed.items():
            self.accessed.setdefault(provider, set()).update(names)
        for provider, names in other.patched.items():
            self.patched.setdefault(provider, set()).update(names)
        self.opaque |= other.opaque


@dataclass
class _Bindings:
    modules: dict[str, Path] = field(default_factory=dict)
    names: dict[str, Path] = field(default_factory=dict)


def _is_private(name: str) -> bool:
    return name.startswith('_') and not name.startswith('__')


def _module_under(root: Path, folder: str, dotted: str) -> Path | None:
    module = root.joinpath(folder, *dotted.split('.')).with_suffix('.py')
    return module if module.is_file() else None


def provider_path(root: Path, dotted: str) -> Path | None:
    """The one launch-root module a bare import name means; a name two roots carry means none."""
    found = [module for folder in PROVIDER_ROOTS if (module := _module_under(root, folder, dotted))]
    return found[0] if len(found) == 1 else None


def is_provider_source(root: Path, path: Path) -> bool:
    return any(path.is_relative_to(root / folder) for folder in PROVIDER_ROOTS)


def _relative_module(root: Path, source: Path, node: ast.ImportFrom) -> str:
    parents = source.relative_to(root / PACKAGE_ROOT).parents
    prefix = '.'.join(parents[node.level - 1].parts)
    return '.'.join(part for part in (prefix, node.module or '') if part)


def imported_module(root: Path, source: Path, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ''
    if not source.is_relative_to(root / PACKAGE_ROOT):
        return ''
    return _relative_module(root, source, node)


def _bind_import(root: Path, node: ast.Import, bindings: _Bindings, found: SourceAccesses) -> None:
    for alias in node.names:
        provider = provider_path(root, alias.name)
        if provider is not None:
            _bind_module(provider, alias, bindings, found)


def _bind_module(provider: Path, alias: ast.alias, bindings: _Bindings, found: SourceAccesses) -> None:
    if alias.asname is None and '.' in alias.name:
        found.opaque.add(provider)
    bindings.modules[alias.asname or alias.name] = provider


def _bind_from_name(root: Path, module: str, alias: ast.alias, bindings: _Bindings, found: SourceAccesses) -> None:
    local = alias.asname or alias.name
    child = provider_path(root, f'{module}.{alias.name}')
    if child is not None:
        bindings.modules[local] = child
        return
    provider = provider_path(root, module)
    if provider is not None:
        _bind_provider_name(provider, alias, local, bindings, found)


def _bind_provider_name(provider: Path, alias: ast.alias, local: str, bindings: _Bindings, found: SourceAccesses) -> None:
    if alias.name == '*':
        found.opaque.add(provider)
        return
    bindings.names[local] = provider
    found.reach(provider, alias.name)


def _bind_from(root: Path, source: Path, node: ast.ImportFrom, bindings: _Bindings, found: SourceAccesses) -> None:
    module = imported_module(root, source, node)
    for alias in node.names:
        _bind_from_name(root, module, alias, bindings, found)


def _bind_node(root: Path, source: Path, node: ast.AST, bindings: _Bindings, found: SourceAccesses) -> None:
    if isinstance(node, ast.Import):
        _bind_import(root, node, bindings, found)
    if isinstance(node, ast.ImportFrom):
        _bind_from(root, source, node, bindings, found)


def _collect_bindings(root: Path, source: Path, tree: ast.AST, found: SourceAccesses) -> _Bindings:
    bindings = _Bindings()
    for node in ast.walk(tree):
        _bind_node(root, source, node, bindings, found)
    return bindings


def _note_attribute(node: ast.Attribute, bindings: _Bindings, found: SourceAccesses) -> None:
    owner = getattr(node.value, 'id', None)
    if owner in bindings.modules:
        found.reach(bindings.modules[owner], node.attr)
        return
    if owner in bindings.names and _is_private(node.attr):
        found.opaque.add(bindings.names[owner])


def _attribute_owners(tree: ast.AST) -> set[int]:
    return {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def _called_name(node: ast.Call) -> str:
    return getattr(node.func, 'attr', getattr(node.func, 'id', ''))


def _calls_named(tree: ast.AST, names: frozenset[str]) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call) and _called_name(node) in names]


def _constant_text(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ''


def _record_patch(provider: Path, name: str, found: SourceAccesses) -> None:
    if name:
        found.patch(provider, name)
    else:
        found.opaque.add(provider)


def _note_module_patch(call: ast.Call, bindings: _Bindings, found: SourceAccesses) -> set[int]:
    target = call.args[0] if call.args else None
    if not isinstance(target, ast.Name) or target.id not in bindings.modules:
        return set()
    name = _constant_text(call.args[1]) if len(call.args) > 1 else ''
    _record_patch(bindings.modules[target.id], name, found)
    return {id(target)}


def _note_named_read(call: ast.Call, bindings: _Bindings, found: SourceAccesses) -> set[int]:
    """`getattr(module, "NAME")` reads one known name, exactly as `module.NAME` does."""
    target, name = (call.args + [None, None])[:2]
    if not isinstance(target, ast.Name) or target.id not in bindings.modules:
        return set()
    text = _constant_text(name) if name is not None else ''
    if not text:
        return set()
    found.reach(bindings.modules[target.id], text)
    return {id(target)}


def _note_named_reads(tree: ast.AST, bindings: _Bindings, found: SourceAccesses) -> set[int]:
    handled: set[int] = set()
    for call in _calls_named(tree, READ_CALLS):
        handled |= _note_named_read(call, bindings, found)
    return handled


def _longest_provider(root: Path, parts: list[str]) -> tuple[Path, int] | None:
    for end in range(len(parts), 0, -1):
        path = provider_path(root, '.'.join(parts[:end]))
        if path is not None:
            return path, end
    return None


def _note_patch_path(root: Path, text: str, found: SourceAccesses) -> None:
    parts = text.split('.')
    match = _longest_provider(root, parts)
    if match is None:
        return
    provider, end = match
    _record_patch(provider, parts[end] if end < len(parts) else '', found)


def _note_patches(root: Path, tree: ast.AST, bindings: _Bindings, found: SourceAccesses) -> set[int]:
    handled: set[int] = set()
    for call in _calls_named(tree, PATCH_CALLS):
        handled |= _note_module_patch(call, bindings, found)
        _note_patch_path(root, _constant_text(call.args[0]) if call.args else '', found)
    return handled


def _note_named_provider(root: Path, text: str, found: SourceAccesses) -> None:
    provider = provider_path(root, text) if text else None
    if provider is not None:
        found.opaque.add(provider)


def _note_import_strings(root: Path, tree: ast.AST, found: SourceAccesses) -> None:
    for call in _calls_named(tree, IMPORT_CALLS):
        for argument in call.args:
            _note_named_provider(root, _constant_text(argument), found)


def _note_path_loads(root: Path, source: Path, found: SourceAccesses) -> None:
    """A module loaded by its file path has no import name, so every use of it is unseen."""
    found.opaque.update(path_loaded_sources(root, syntax_nodes(source)))


def _loaded_names(tree: ast.AST) -> list[ast.Name]:
    names = [node for node in ast.walk(tree) if isinstance(node, ast.Name)]
    return [node for node in names if isinstance(node.ctx, ast.Load)]


def _note_bare_modules(tree: ast.AST, bindings: _Bindings, handled: set[int], found: SourceAccesses) -> None:
    settled = _attribute_owners(tree) | handled
    bare = [node for node in _loaded_names(tree) if id(node) not in settled]
    found.opaque.update(bindings.modules[node.id] for node in bare if node.id in bindings.modules)


def source_accesses(root: Path, source: Path) -> SourceAccesses:
    found = SourceAccesses()
    try:
        tree = ast.parse(source.read_bytes())
    except SyntaxError:
        return found
    bindings = _collect_bindings(root, source, tree, found)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            _note_attribute(node, bindings, found)
    handled = _note_patches(root, tree, bindings, found) | _note_named_reads(tree, bindings, found)
    _note_import_strings(root, tree, found)
    _note_path_loads(root, source, found)
    _note_bare_modules(tree, bindings, handled, found)
    return found


def packet_accesses(root: Path, files: set[Path]) -> SourceAccesses:
    total = SourceAccesses()
    for path in sorted(p for p in files if p.suffix == '.py'):
        total.merge(source_accesses(root, path))
    return total
