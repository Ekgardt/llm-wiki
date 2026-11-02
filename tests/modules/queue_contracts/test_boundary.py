"""Consumers depend on queue data contracts, not their executor."""
import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
NAMES = frozenset(json.loads((Path(__file__).parent / 'fixtures/legacy-pickle.json').read_text())['names'])


def _legacy_import(node):
    if not isinstance(node, ast.ImportFrom) or node.module != 'memory_queue':
        return False
    return any(alias.name in NAMES or alias.name == '*' for alias in node.names)


def _module_aliases(node):
    if not isinstance(node, ast.Import):
        return set()
    return {alias.asname or alias.name for alias in node.names if alias.name == 'memory_queue'}


def _queue_aliases(tree):
    return set().union(*(_module_aliases(node) for node in ast.walk(tree)))


def _qualified_type(node, aliases):
    if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
        return False
    return node.value.id in aliases and node.attr in NAMES


def _violations(source):
    tree = ast.parse(source)
    aliases = _queue_aliases(tree)
    return [node.lineno for node in ast.walk(tree)
            if _legacy_import(node) or _qualified_type(node, aliases)]


def test_consumers_import_queue_types_from_their_owner():
    offenders = {}
    for path in (ROOT / 'scripts').rglob('*.py'):
        bad = _violations(path.read_text(encoding='utf-8'))
        if bad:
            offenders[str(path.relative_to(ROOT))] = bad
    assert not offenders, offenders


@pytest.mark.parametrize('source', [
    'from memory_queue import QueueFailure',
    'from memory_queue import QueueFailure as Failure',
    'from memory_queue import *',
    'import memory_queue; failure = memory_queue.QueueFailure',
    'import memory_queue as queue; failure = queue.QueueFailure',
    'def local():\n    from memory_queue import QueueFailure',
])
def test_guard_detects_executor_dependency(source):
    assert _violations(source)


@pytest.mark.parametrize('source', [
    'from memory_queue import MemoryQueue',
    'import memory_queue as queue; queue.active_or_legacy_memory_queue()',
    'from llm_wiki.queue_contracts import QueueFailure',
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
    tree = ast.parse((ROOT / 'scripts/llm_wiki/queue_contracts.py').read_text(encoding='utf-8'))
    roots = set().union(*(_import_roots(node) for node in ast.walk(tree)))
    assert roots <= sys.stdlib_module_names
