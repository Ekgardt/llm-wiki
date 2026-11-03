"""Existing storage owns no queue/controller runtime or database connection."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/compile-progress-context.json')


def test_storage_source_has_only_declared_lower_dependencies():
    source = read_source(ROOT / 'scripts/compile_progress.py')
    assert nonstdlib_imports(source) == {
        'llm_wiki.sqlite_deadlines', 'llm_wiki.formats.source',
    }


def test_storage_import_does_not_load_queue_or_controller():
    assert_isolated_import(ROOT, 'compile_progress', dependencies=(
        'llm_wiki', 'llm_wiki.sqlite_deadlines',
    ))
