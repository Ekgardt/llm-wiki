"""Cleanup and its owned tests cannot depend on the queue executor."""
import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
ALLOWED = sys.stdlib_module_names | {'pytest', 'tiktoken', 'process_liveness'}


def _imports(node):
    if isinstance(node, ast.ImportFrom):
        return {(node.module or '', node.level)}
    if isinstance(node, ast.Import):
        return {(alias.name, 0) for alias in node.names}
    return set()


def _allowed(name, level, test_helpers=()):
    if level:
        return False
    return name.split('.')[0] in ALLOWED or name in {
        'llm_wiki.queue_contracts', 'llm_wiki.queue_process_cleanup',
        'llm_wiki.queue_child_runtime', 'tests.slow_machine',
    } | set(test_helpers)


def _violations(source, test_helpers=()):
    imports = set().union(*(_imports(node) for node in ast.walk(ast.parse(source))))
    return sorted(name for name, level in imports if not _allowed(name, level, test_helpers))


def test_owned_tests_do_not_import_executors():
    findings = {path.name: _violations(path.read_text(encoding='utf-8'), {'tests.module_context'})
                for path in Path(__file__).parent.glob('*.py')}
    assert not {path: bad for path, bad in findings.items() if bad}


def test_cleanup_source_does_not_import_executors():
    path = ROOT / 'scripts/llm_wiki/queue_child_runtime.py'
    assert not _violations(path.read_text(encoding='utf-8'))


def _assigned_names(tree):
    stores = [node for node in ast.walk(tree) if isinstance(node, ast.Name)]
    return {node.id for node in stores if isinstance(node.ctx, ast.Store)}


def test_result_limit_is_not_duplicated_in_transport():
    path = ROOT / 'scripts/llm_wiki/queue_child_runtime.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    assert '_MAX_RESULT_BYTES' not in _assigned_names(tree)


def test_limit_guard_detects_a_shadowed_import():
    tree = ast.parse('from llm_wiki.queue_contracts import _MAX_RESULT_BYTES\n_MAX_RESULT_BYTES = 16')
    assert '_MAX_RESULT_BYTES' in _assigned_names(tree)


@pytest.mark.parametrize('source', [
    'import memory_queue', 'import memory_state as state',
    'from memory_queue import *',
    'def f():\n    from markdown_transaction import mutate_knowledge',
    'from . import memory_queue', 'from llm_wiki import memory_queue',
])
def test_guard_rejects_executor_dependency(source):
    assert _violations(source)


@pytest.mark.parametrize('source', [
    'import os', 'from ctypes import wintypes',
    'from llm_wiki.queue_contracts import QueueOperationError',
    'from process_liveness import pid_alive',
])
def test_guard_accepts_owned_dependencies(source):
    assert not _violations(source)


def test_measurement_helper_is_test_only():
    source = 'from tests.module_context import measure_context'
    assert _violations(source)
    assert not _violations(source, {'tests.module_context'})
