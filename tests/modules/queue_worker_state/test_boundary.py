"""Worker value policy cannot import queue execution or runtime coordination."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/queue-worker-state-context.json')


def test_worker_value_import_is_isolated():
    assert_isolated_import(ROOT, 'llm_wiki.queue_worker_state', dependencies=(
        'llm_wiki', 'llm_wiki.queue_contracts', 'llm_wiki.reliability_defaults',
        'llm_wiki.utf8_text',
    ))


def test_worker_values_depend_only_on_lower_contracts():
    source = read_source(ROOT / 'scripts/llm_wiki/queue_worker_state.py')
    assert nonstdlib_imports(source) == {
        'llm_wiki.queue_contracts', 'llm_wiki.reliability_defaults', 'llm_wiki.utf8_text',
    }
