"""Connection validation depends only on lower schema and pragma contracts."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/queue-database-contract-context.json')


def test_no_executor_or_path_opener_is_imported():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/llm_wiki/queue_database_contract.py')) == {
        'llm_wiki.compile_journal_schema', 'llm_wiki.formats.canonical',
        'llm_wiki.queue_schema', 'llm_wiki.sqlite_contracts', 'llm_wiki.sqlite_pragmas',
    }


def test_import_is_independent_of_queue_journal_and_file_io():
    assert_isolated_import(ROOT, 'llm_wiki.queue_database_contract', dependencies=(
        'llm_wiki', 'llm_wiki.compile_journal_schema', 'llm_wiki.compile_event_records',
        'llm_wiki.formats', 'llm_wiki.formats.canonical', 'llm_wiki.queue_schema',
        'llm_wiki.sqlite_contracts', 'llm_wiki.sqlite_pragmas',
    ))
