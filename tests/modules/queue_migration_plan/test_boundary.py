"""Migration plans cannot pull in their executor or acquire connections."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_executor_no_longer_defines_migration_plan():
    tree = ast.parse((ROOT / 'scripts/memory_queue.py').read_text())
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert not names.intersection({'_queue_v2_migration_statements',
                                   '_queue_v2_reconciliation_complete',
                                   '_ordered_queue_v2_tasks'})


def test_owner_imports_only_declared_dependencies():
    source = ROOT / 'scripts/llm_wiki/queue_migration_plan.py'
    tree = ast.parse(source.read_text())
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imports <= {'__future__', 'collections.abc', 'functools',
                       'llm_wiki.formats.canonical', 'llm_wiki.formats.queue_payload',
                       'llm_wiki.queue_sources', 'llm_wiki.sqlite_contracts'}


def test_owner_does_not_open_database_or_import_executor():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/queue_migration_plan.py').read_text())
    imports = [n for n in ast.walk(tree) if isinstance(n, ast.Import)]
    assert {a.name for n in imports for a in n.names} <= {'sqlite3'}


def test_owner_does_not_manage_connection_lifecycle():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/queue_migration_plan.py').read_text())
    calls = [n.func for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert not {n.attr for n in calls if isinstance(n, ast.Attribute)}.intersection(
        {'connect', 'commit', 'rollback', 'close'})
