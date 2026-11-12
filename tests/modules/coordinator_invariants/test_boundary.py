"""Supplied-connection validation cannot load the coordinator or execution owners."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/coordinator-invariants-context.json')


def test_validation_uses_only_schema_and_value_contracts():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/llm_wiki/coordinator_invariants.py')) == {
        'llm_wiki.coordinator_schema', 'llm_wiki.sqlite_contracts',
    }
    assert_isolated_import(ROOT, 'llm_wiki.coordinator_invariants', dependencies=(
        'llm_wiki', 'llm_wiki.coordinator_schema', 'llm_wiki.sqlite_contracts',
        'llm_wiki.sqlite_schema_text', 'llm_wiki.formats', 'llm_wiki.formats.canonical',
    ))
