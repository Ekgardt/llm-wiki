"""Resolve real Python source without importing or executing that source."""
from pathlib import Path

import import_resolver as resolver


def _write(root: Path, name: str, body: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding='utf-8')
    return path


def test_workspace_evidence_confirms_import_but_never_executes_the_module(tmp_path):
    _write(tmp_path, 'library.py', 'raise RuntimeError("must not execute")\ndef target(): pass\n')
    source = _write(tmp_path, 'client.py', 'from library import target as alias\nalias()\n')
    imports, without = resolver.resolve_python_imports_and_calls(source)
    registry = resolver.build_python_symbol_registry(tmp_path)
    _, with_registry = resolver.resolve_python_imports_and_calls(source, registry, tmp_path)
    assert imports == [{'name': 'target', 'local_name': 'alias', 'module': 'library',
                        'line': 1, 'end_line': 1}]
    assert (without[0]['qualified_name'], without[0]['confidence'], without[0]['unresolved_reason']) == (
        'library.target', 'unknown', 'missing_import_target',
    )
    assert (with_registry[0]['qualified_name'], with_registry[0]['confidence'],
            with_registry[0]['line'], with_registry[0]['evidence']) == (
        'library.target', 'confirmed', 2, 'from_import',
    )


def test_rebinding_an_import_preserves_unknown_target_instead_of_a_false_edge(tmp_path):
    _write(tmp_path, 'library.py', 'def target(): pass\n')
    source = _write(tmp_path, 'client.py', 'from library import target\ntarget = other\ntarget()\n')
    registry = resolver.build_python_symbol_registry(tmp_path)
    _, calls = resolver.resolve_python_imports_and_calls(source, registry, tmp_path)
    assert (calls[0]['qualified_name'], calls[0]['confidence'], calls[0]['unresolved_reason']) == (
        None, 'unknown', 'shadowed_binding',
    )


def test_one_invalid_file_does_not_hide_its_valid_neighbour(tmp_path):
    bad = _write(tmp_path, 'bad.py', 'def bad(:\n')
    _write(tmp_path, 'good.py', 'def intact(): pass\n')
    registry = resolver.build_python_symbol_registry(tmp_path)
    assert registry.modules == frozenset({'good'})
    assert registry.symbols == frozenset({'good.intact'})
    assert resolver.resolve_python_imports_and_calls(bad) == ([], [])


def test_file_admission_retains_the_existing_inclusive_size_bound(tmp_path):
    source = tmp_path / 'source.py'
    with source.open('wb') as stream:
        stream.truncate(resolver.LIVE_SOURCE_MAX_BYTES)
    assert resolver.own_source_file(source)
    with source.open('ab') as stream:
        stream.write(b'x')
    assert not resolver.own_source_file(source)
    assert not resolver.own_source_file(tmp_path)
    assert not resolver.own_source_file(tmp_path / 'missing.py')
