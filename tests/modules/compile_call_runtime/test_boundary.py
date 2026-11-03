"""Call lifecycle imports value and journal owners, never the executor."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/compile-call-runtime-context.json')


def test_only_explicit_lower_contracts_are_imported():
    source = read_source(ROOT / 'scripts/llm_wiki/compile_call_runtime.py')
    assert nonstdlib_imports(source) == {
        'compile_progress', 'llm_wiki.compile_answer_records', 'llm_wiki.queue_contracts',
    }


def test_import_cannot_load_controller_queue_or_transport():
    assert_isolated_import(ROOT, 'llm_wiki.compile_call_runtime', dependencies=(
        'llm_wiki', 'compile_progress', 'llm_wiki.compile_answer_records',
        'llm_wiki.queue_contracts', 'llm_wiki.model_records', 'context_budget',
        'llm_wiki.sqlite_deadlines', 'llm_wiki.formats', 'llm_wiki.formats.canonical',
        'llm_wiki.formats.source',
    ))
