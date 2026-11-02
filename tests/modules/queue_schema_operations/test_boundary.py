"""Schema operations have one owner and cannot import the queue executor."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
NAMES = {'_normalized_sql', '_queue_v3_object_matches', '_queue_v3_statements',
         '_queue_v3_schema_complete', '_expected_queue_v3_schema', '_queue_schema_rows',
         '_drop_order_key', '_drop_schema_object', '_drop_unexpected_objects',
         '_table_is_populated', '_check_no_populated_rebuild',
         '_malformed_queue_v3_objects', '_repair_partial_queue_v3_schema',
         '_queue_v3_trigger_statements', '_migration_error'}


def test_executor_no_longer_defines_schema_operations():
    tree = ast.parse((ROOT / 'scripts/memory_queue.py').read_text())
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert not names.intersection(NAMES)


def test_schema_owner_uses_only_declared_dependencies():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/queue_schema_operations.py').read_text())
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imports <= {'__future__', 'collections.abc', 'llm_wiki.queue_schema',
                       'llm_wiki.sqlite_contracts', 'llm_wiki.sqlite_transactions'}


def test_direct_imports_do_not_hide_an_executor():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/queue_schema_operations.py').read_text())
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Import)]
    assert {a.name for n in nodes for a in n.names} <= {'sqlite3'}
