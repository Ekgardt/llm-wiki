"""Validation imports value dependencies without loading execution owners."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/ownership-validation-context.json')


def test_value_dependencies_only():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/llm_wiki/ownership_validation.py')) == {
        'process_liveness', 'llm_wiki.formats.canonical', 'llm_wiki.ownership_contracts',
        'llm_wiki.restricted_paths', 'llm_wiki.runtime_contracts',
    }
    assert_isolated_import(ROOT, 'llm_wiki.ownership_validation', dependencies=(
        'llm_wiki', 'process_liveness', 'llm_wiki.formats', 'llm_wiki.formats.canonical',
        'llm_wiki.ownership_contracts', 'llm_wiki.restricted_paths', 'llm_wiki.runtime_contracts',
    ))
