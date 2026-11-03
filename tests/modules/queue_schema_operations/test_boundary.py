"""Schema operations have one owner and cannot import the queue executor."""
import ast
from pathlib import Path

from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/queue-schema-operations-context.json')


def test_schema_owner_uses_only_declared_dependencies():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/queue_schema_operations.py'))
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imports <= {'__future__', 'collections.abc', 'llm_wiki.queue_schema',
                       'llm_wiki.sqlite_contracts', 'llm_wiki.sqlite_transactions',
                       'llm_wiki.sqlite_schema_text'}


def test_direct_imports_do_not_hide_an_executor():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/queue_schema_operations.py'))
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Import)]
    assert {a.name for n in nodes for a in n.names} <= {'sqlite3'}
