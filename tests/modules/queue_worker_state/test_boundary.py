"""Worker value policy cannot import queue execution or runtime coordination."""
import ast
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports

ROOT = Path(__file__).resolve().parents[3]


def test_worker_value_import_is_isolated():
    assert_isolated_import(ROOT, 'llm_wiki.queue_worker_state', dependencies=(
        'llm_wiki', 'llm_wiki.queue_contracts', 'llm_wiki.reliability_defaults',
        'llm_wiki.utf8_text',
    ))


def test_worker_values_depend_only_on_lower_contracts():
    source = (ROOT / 'scripts/llm_wiki/queue_worker_state.py').read_text()
    assert nonstdlib_imports(source) == {
        'llm_wiki.queue_contracts', 'llm_wiki.reliability_defaults', 'llm_wiki.utf8_text',
    }


def test_queue_has_no_second_worker_value_implementation():
    tree = ast.parse((ROOT / 'scripts/memory_queue.py').read_text())
    names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert not names.intersection({'QueueRetryPolicy', '_ProcessorOutcome', '_WorkerProgress', 'processor_error_code'})


def test_capture_reason_conversion_does_not_import_queue_executor():
    tree = ast.parse((ROOT / 'scripts/flush_memory.py').read_text())
    function = next(n for n in tree.body if getattr(n, 'name', None) == '_capture_queue_failure')
    assert isinstance(function, ast.FunctionDef)
    imports = {n.module for n in ast.walk(function) if isinstance(n, ast.ImportFrom)}
    assert 'memory_queue' not in imports
    assert 'llm_wiki.queue_worker_state' in imports
