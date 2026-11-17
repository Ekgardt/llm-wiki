"""Schema inspection imports no coordinator, migration runner or filesystem owner."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/coordinator-schema-context.json')


def test_dependencies_are_only_declarations_formats_and_sql_text():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/llm_wiki/coordinator_schema.py')) == {
        'llm_wiki.formats.canonical', 'llm_wiki.sqlite_contracts', 'llm_wiki.sqlite_schema_text',
    }
    assert_isolated_import(ROOT, 'llm_wiki.coordinator_schema', dependencies=(
        'llm_wiki', 'llm_wiki.formats', 'llm_wiki.formats.canonical',
        'llm_wiki.sqlite_contracts', 'llm_wiki.sqlite_schema_text',
    ))
