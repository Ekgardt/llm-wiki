"""Consumers read immutable defaults from their owner, without durability imports."""
import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
NAMES = frozenset(json.loads((Path(__file__).parent / 'fixtures/legacy-pickle.json').read_text())['names'])


def _legacy_import(node):
    if not isinstance(node, ast.ImportFrom) or node.module != 'reliable_memory':
        return False
    return any(alias.name in NAMES or alias.name == '*' for alias in node.names)


def _module_aliases(node):
    if not isinstance(node, ast.Import):
        return set()
    return {alias.asname or alias.name for alias in node.names if alias.name == 'reliable_memory'}


def _facade_aliases(tree):
    return set().union(*(_module_aliases(node) for node in ast.walk(tree)))


def _qualified_type(node, aliases):
    if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
        return False
    return node.value.id in aliases and node.attr in NAMES


def _violations(source):
    tree = ast.parse(source)
    aliases = _facade_aliases(tree)
    return [node.lineno for node in ast.walk(tree)
            if _legacy_import(node) or _qualified_type(node, aliases)]


def test_consumers_import_defaults_from_their_owner():
    offenders = {}
    for path in (ROOT / 'scripts').rglob('*.py'):
        bad = _violations(path.read_text(encoding='utf-8'))
        if bad:
            offenders[str(path.relative_to(ROOT))] = bad
    assert not offenders, offenders


@pytest.mark.parametrize('source', [
    'from reliable_memory import DEFAULTS',
    'from reliable_memory import ReliableMemoryDefaults',
    'import reliable_memory as runtime; runtime.ReliableMemoryDefaults()',
    'from reliable_memory import DEFAULTS as Failure',
    'from reliable_memory import *',
    'import reliable_memory; failure = reliable_memory.DEFAULTS',
    'import reliable_memory as queue; failure = queue.DEFAULTS',
    'def local():\n    from reliable_memory import DEFAULTS',
])
def test_guard_detects_executor_dependency(source):
    assert _violations(source)


@pytest.mark.parametrize('source', [
    'from reliable_memory import open_operational_db',
    'import reliable_memory as queue; queue.open_operational_db()',
    'from llm_wiki.reliability_defaults import DEFAULTS',
])
def test_guard_allows_operations_and_owned_types(source):
    assert not _violations(source)


def _import_roots(node):
    if isinstance(node, ast.ImportFrom):
        assert node.level == 0
        return {node.module.split('.')[0]}
    if isinstance(node, ast.Import):
        return {alias.name.split('.')[0] for alias in node.names}
    return set()


def test_contract_source_imports_only_standard_library():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/reliability_defaults.py').read_text(encoding='utf-8'))
    roots = set().union(*(_import_roots(node) for node in ast.walk(tree)))
    assert roots <= sys.stdlib_module_names
