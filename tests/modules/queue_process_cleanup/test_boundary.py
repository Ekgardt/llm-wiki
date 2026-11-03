"""Cleanup and its owned tests cannot depend on the queue executor."""
import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/queue-process-cleanup-context.json')
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
    } | set(test_helpers)


def _violations(source, test_helpers=()):
    imports = set().union(*(_imports(node) for node in ast.walk(ast.parse(source))))
    return sorted(name for name, level in imports if not _allowed(name, level, test_helpers))


def test_owned_tests_do_not_import_executors():
    findings = {path.name: _violations(read_source(path, encoding='utf-8'), {'tests.module_context'})
                for path in Path(__file__).parent.glob('*.py')}
    assert not {path: bad for path, bad in findings.items() if bad}


def test_cleanup_source_does_not_import_executors():
    path = ROOT / 'scripts/llm_wiki/queue_process_cleanup.py'
    assert not _violations(read_source(path, encoding='utf-8'))


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


def test_import_and_process_probe_work_without_executor():
    code = '''
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'memory_queue', 'memory_state', 'markdown_transaction'}:
        raise AssertionError('executor import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import llm_wiki.queue_process_cleanup as cleanup
import process_liveness
process_liveness.process_state = lambda pid: 'dead'
assert cleanup._pid_is_alive(4242) is False
assert cleanup._pid_is_alive(True) is False
cleanup._windows_process_pairs = lambda: [(11, 5), (12, 11)]
assert cleanup._tracked_descendant_pids(5, 'nt') == {11, 12}
assert cleanup._cleanup_failed(False, 'nt', True)
assert not cleanup._cleanup_failed(True, 'nt', True)
assert cleanup._terminate_processor_child.__kwdefaults__['tracked_descendants'] is cleanup._DISCOVER_DESCENDANTS
'''
    result = subprocess.run(
        [sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, 'PYTHONPATH': str(ROOT / 'scripts')}, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_measurement_helper_is_test_only():
    source = 'from tests.module_context import measure_context'
    assert _violations(source)
    assert not _violations(source, {'tests.module_context'})
