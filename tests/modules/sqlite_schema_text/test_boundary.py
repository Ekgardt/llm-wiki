"""Schema text has no dependency on database or runtime owners."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/sqlite-schema-text-context.json')


def test_normalizer_uses_only_standard_library():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/llm_wiki/sqlite_schema_text.py')) == set()


def test_import_does_not_load_database_or_runtime_owners():
    assert_isolated_import(ROOT, 'llm_wiki.sqlite_schema_text', dependencies=('llm_wiki',))
