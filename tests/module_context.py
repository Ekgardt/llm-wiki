"""Measure declared maintenance bundles, including their implicit pytest loader."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import tiktoken
from llm_wiki.context_bundle import pack_files, unpack_files

from tests.module_context_paths import _contained as _contained
from tests.module_record_context import context_record_files, selected_entries


def _source_file(path: Path) -> bool:
    return path.is_file() and '__pycache__' not in path.parts


def context_files(root: Path, manifest: dict) -> set[Path]:
    files = {_contained(root, name) for name in manifest['files']}
    for name in manifest.get('directories', ()):
        folder = _contained(root, name)
        assert folder.is_dir(), f'Missing context directory: {name}'
        files.update(_directory_files(root, folder))
    assert all(p.is_file() for p in files), 'Missing declared context file'
    return files


def _directory_files(root: Path, folder: Path) -> set[Path]:
    paths = [p for p in folder.rglob('*') if _source_file(p)]
    return {_contained(root, str(p.relative_to(root))) for p in paths}


def _conftests(root: Path, path: Path) -> set[Path]:
    candidates = [root / parent / 'conftest.py' for parent in path.relative_to(root).parents]
    return {p for p in candidates if p.is_file()}


def _assignment_targets(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.Assign):
        return node.targets
    if isinstance(node, ast.AnnAssign):
        return [node.target]
    return []


def _plugin_value(node: ast.AST):
    names = [getattr(t, 'id', None) for t in _assignment_targets(node)]
    if 'pytest_plugins' in names:
        return node.value
    return None


def _plugin_names(path: Path) -> list[str]:
    values = [_plugin_value(node) for node in ast.walk(ast.parse(path.read_bytes()))]
    names = []
    for value in values:
        if value is not None:
            names.extend(_literal_plugins(value))
    return names


def _literal_plugins(value: ast.AST) -> list[str]:
    declared = ast.literal_eval(value)
    if isinstance(declared, str):
        return [declared]
    assert isinstance(declared, (list, tuple)), 'Unsupported pytest_plugins declaration'
    assert all(isinstance(name, str) for name in declared), 'Plugin names must be strings'
    return list(declared)


def _local_plugin(root: Path, name: str) -> set[Path]:
    parts = name.split('.')
    if not (root / parts[0]).exists():
        return set()  # Installed dependencies are declared by pyproject/lockfile.
    base = Path(*parts)
    candidates = [base.with_suffix('.py'), base / '__init__.py']
    matches = {_contained(root, str(p)) for p in candidates if (root / p).is_file()}
    assert matches, f'Missing local pytest plugin: {name}'
    return matches


def required_bootstrap(root: Path, files: set[Path]) -> set[Path]:
    pending = set().union(*(_conftests(root, p) for p in files))
    seen: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        pending.update(_plugin_files(root, path) - seen)
    return seen


def _plugin_files(root: Path, path: Path) -> set[Path]:
    return set().union(*(_local_plugin(root, name) for name in _plugin_names(path)))


def _context_python(root: Path, path: Path) -> bool:
    return path.suffix == '.py' and path.is_relative_to(root)


def _package_initializers(root: Path, base: Path) -> set[Path]:
    parents = [p for p in base.parents if p.is_relative_to(root)]
    candidates = [p / '__init__.py' for p in parents]
    return _existing_paths(candidates)


def _existing_paths(candidates) -> set[Path]:
    return {p for p in candidates if p.is_file()}


def _local_source_files(root: Path, base: Path) -> set[Path]:
    found = _existing_paths([base.with_suffix('.py'), base / '__init__.py'])
    if not found and not base.is_dir():
        return set()  # Installed or stdlib import, not a source under scripts/.
    found.update(_package_initializers(root, base))
    return {_contained(root, str(p.relative_to(root))) for p in found}


def _relative_import_parent(root: Path, source: Path, level: int) -> Path:
    parent = source.parent
    for _ in range(level - 1):
        parent = parent.parent
    assert parent.is_relative_to(root), 'Relative import escapes repository'
    return parent


def _absolute_import_bases(root: Path, source: Path, name: str) -> set[Path]:
    """Current launch roots, conservatively retaining ambiguous local names."""
    parts = name.split('.')
    roots = {root, root / 'scripts', root / 'tests', root / 'benchmark', source.parent}
    return {folder.joinpath(*parts) for folder in roots}


def _from_import_bases(root: Path, source: Path, node: ast.ImportFrom) -> set[Path]:
    if node.level:
        parent = _relative_import_parent(root, source, node.level)
        return {parent.joinpath(*(node.module or '').split('.'))}
    return _absolute_import_bases(root, source, node.module or '')


def _from_import_sources(root: Path, source: Path, node: ast.ImportFrom) -> set[Path]:
    bases = _from_import_bases(root, source, node)
    children = {base / alias.name for base in bases for alias in node.names}
    return set().union(*(_local_source_files(root, base) for base in bases | children))


def _import_sources(root: Path, source: Path, node: ast.AST) -> set[Path]:
    if isinstance(node, ast.Import):
        bases = set().union(*(_absolute_import_bases(root, source, a.name) for a in node.names))
        return set().union(*(_local_source_files(root, base) for base in bases))
    if isinstance(node, ast.ImportFrom):
        return _from_import_sources(root, source, node)
    return set()


def _source_dependencies(root: Path, source: Path) -> set[Path]:
    nodes = ast.walk(ast.parse(source.read_bytes()))
    imports = set().union(*(_import_sources(root, source, node) for node in nodes))
    parents = _package_initializers(root, source.with_suffix(''))
    return imports | {_contained(root, str(p.relative_to(root))) for p in parents}


def required_source_context(root: Path, files: set[Path]) -> set[Path]:
    """Product and test/support imports, apart from declared pytest infrastructure."""
    bootstrap = required_bootstrap(root, files)
    pending = {p for p in files if _context_python(root, p)} - bootstrap
    seen: set[Path] = set()
    while pending:
        source = pending.pop()
        if source in seen:
            continue
        seen.add(source)
        pending.update(_source_dependencies(root, source) - seen - bootstrap)
    return seen


def _whole_entries(root: Path, files: set[Path]) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(files)}


def context_entries(root: Path, files: set[Path], manifest: dict) -> dict[str, bytes]:
    entries = _whole_entries(root, files)
    assert not any(name.startswith('@record/') for name in entries), 'Reserved record entry path'
    entries.update(selected_entries(root, manifest))
    return entries


def _raw_entries(entries: dict[str, bytes]) -> str:
    return ''.join(f'FILE {name} {len(content)} bytes\n' + content.decode('utf-8') + '\n'
                   for name, content in entries.items())


def serialized_bundle(root: Path, files: set[Path]) -> str:
    return _raw_entries(_whole_entries(root, files))


def _packed_entries(originals: dict[str, bytes]) -> str:
    bundle = pack_files(originals)
    assert unpack_files(bundle) == originals, 'Maintenance bundle lost file content'
    return bundle


def compact_bundle(root: Path, files: set[Path]) -> str:
    return _packed_entries(_whole_entries(root, files))


def _manifest_bundle(root: Path, files: set[Path], manifest: dict) -> str:
    assert 'qualified_dependencies' not in manifest, 'Qualified dependencies require fresh qualification'
    serializers = {'raw/v1': _raw_entries, 'exact-duplicates/v1': _packed_entries}
    kind = manifest.get('serialization', 'raw/v1')
    assert kind in serializers, f'Unknown maintenance serialization: {kind}'
    return serializers[kind](context_entries(root, files, manifest))


def relative_context_names(root, paths):
    """Manifest and diagnostic paths have the same separators on every host."""
    return sorted(path.relative_to(root).as_posix() for path in paths)


def validate_context(root: Path, manifest_path: Path, manifest: dict) -> set[Path]:
    assert manifest['max_tokens'] == 100000, 'Owner limit cannot be changed'
    assert manifest['encoding'] == 'o200k_base', 'Measurement encoding cannot drift'
    files = context_files(root, manifest)
    authority = {root / name for name in ('AGENTS.md', 'CLAUDE.md', 'rules/development-laws.md',
                                         'docs/AGENTS.md', 'docs/STRUCTURE.md')}
    needed = authority | context_record_files(root, manifest) | required_bootstrap(root, files) | required_source_context(root, files) | {manifest_path.resolve(), root / 'tests/module_context.py',
                                                       root / 'scripts/llm_wiki/context_bundle.py',
                                                       root / 'docs/modules/context-tooling.md'}
    assert needed <= files, f'Missing required context: {relative_context_names(root, needed - files)}'
    return files


def measure_context(root: Path, manifest_path: Path) -> tuple[int, int]:
    root = root.resolve()
    manifest = json.loads(manifest_path.read_bytes())
    files = validate_context(root, manifest_path, manifest)
    total = len(tiktoken.get_encoding(manifest['encoding']).encode_ordinary(_manifest_bundle(root, files, manifest)))
    assert total <= manifest['max_tokens'], f'{manifest_path.name}: {total} tokens'
    return total, len(files)


def _read_declared_source(root: Path, manifest_path: Path, path: Path, *, encoding='utf-8') -> str:
    target = path.resolve()
    assert target.is_relative_to(root.resolve()), f'Undeclared context read: {path}'
    manifest = json.loads(manifest_path.read_bytes())
    declared = context_files(root.resolve(), manifest)
    assert target in declared, f'Undeclared context read: {path}'
    return target.read_text(encoding=encoding)


def context_reader(root: Path, manifest_path: Path):
    """Bind source inspection to the same whole-file declaration that is measured."""
    from functools import partial

    return partial(_read_declared_source, root, manifest_path)
