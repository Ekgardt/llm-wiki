"""Artifact IO has one owner and never imports queue execution or SQL."""
import ast
from pathlib import Path

import memory_queue
from llm_wiki import queue_artifacts

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports

ROOT = Path(__file__).resolve().parents[3]


def _definitions(path):
    tree = ast.parse((ROOT / path).read_text())
    return {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}


def test_legacy_names_are_identical_and_have_no_second_implementation():
    names = _definitions('scripts/llm_wiki/queue_artifacts.py')
    definitions = _definitions('scripts/memory_queue.py')
    assert not names & definitions
    for name in names:
        assert getattr(memory_queue, name) is getattr(queue_artifacts, name)


def test_only_explicit_lower_dependencies_including_lazy_imports():
    source = (ROOT / 'scripts/llm_wiki/queue_artifacts.py').read_text()
    assert nonstdlib_imports(source) <= {
        'llm_wiki.queue_contracts', 'llm_wiki.runtime_environment',
        'llm_wiki.runtime_sync', 'llm_wiki.sqlite_deadlines', 'llm_wiki.windows_acl',
    }


def test_import_does_not_load_execution_or_compile_journal():
    assert_isolated_import(ROOT, 'llm_wiki.queue_artifacts', dependencies=(
        'llm_wiki', 'llm_wiki.queue_contracts', 'llm_wiki.runtime_environment',
        'llm_wiki.runtime_sync', 'llm_wiki.runtime_contracts',
        'llm_wiki.sqlite_deadlines', 'llm_wiki.windows_acl', 'llm_wiki.windows_native',
        'windows_workspace',
    ))
