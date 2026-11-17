"""Pure event values cannot regain a journal or runtime dependency."""
import ast
from pathlib import Path

from tests.module_boundaries import _imports, assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/compile-event-records-context.json')


def test_event_values_have_no_product_or_sqlite_import():
    source = read_source(ROOT / 'scripts/llm_wiki/compile_event_records.py')
    assert not nonstdlib_imports(source)
    names = set().union(*(_imports(node) for node in ast.walk(ast.parse(source))))
    assert ('sqlite3', 0) not in names


def test_import_is_independent_of_journal_queue_and_controller():
    assert_isolated_import(ROOT, 'llm_wiki.compile_event_records', dependencies=('llm_wiki',))
