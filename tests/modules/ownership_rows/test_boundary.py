"""Row helpers cannot import registry, transaction, filesystem or process executors."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/ownership-rows-context.json')


def test_row_helpers_have_only_value_and_time_dependencies():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/llm_wiki/ownership_rows.py')) == {
        'llm_wiki.ownership_contracts', 'llm_wiki.ownership_timing',
    }
    assert_isolated_import(ROOT, 'llm_wiki.ownership_rows', dependencies=(
        'llm_wiki', 'llm_wiki.ownership_contracts', 'llm_wiki.ownership_timing',
        'llm_wiki.runtime_contracts', 'iso_time',
    ))
