"""Protected schema checks do not import storage or queue executors."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/compile-journal-schema-context.json')


def test_schema_imports_only_pure_event_errors():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/llm_wiki/compile_journal_schema.py')) == {
        'llm_wiki.compile_event_records',
    }


def test_import_does_not_load_journal_queue_or_file_io():
    assert_isolated_import(ROOT, 'llm_wiki.compile_journal_schema', dependencies=(
        'llm_wiki', 'llm_wiki.compile_event_records',
    ))
