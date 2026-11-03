"""Representation depends downward and never owns queue execution or SQL."""
import ast
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / 'scripts/llm_wiki/queue_task_records.py'


def test_representation_has_only_the_existing_lower_contracts():
    assert nonstdlib_imports(SOURCE.read_text()) == {
        'llm_wiki.queue_contracts', 'llm_wiki.queue_timing', 'llm_wiki.formats.schema',
    }


def test_representation_import_cannot_load_the_queue():
    assert_isolated_import(ROOT, 'llm_wiki.queue_task_records', dependencies=(
        'llm_wiki', 'llm_wiki.queue_contracts', 'llm_wiki.queue_timing',
        'llm_wiki.formats', 'llm_wiki.formats.schema', 'llm_wiki.reliability_defaults',
    ))


def test_sql_execution_stays_outside_the_representation_owner():
    tree = ast.parse(SOURCE.read_text())
    attributes = {node.func.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not attributes & {'connect', 'execute', 'executemany', 'commit', 'rollback'}


def test_required_queue_calls_use_aliases_not_duplicate_implementations():
    tree = ast.parse((ROOT / 'scripts/memory_queue.py').read_text())
    names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    local = ast.parse(SOURCE.read_text())
    owned = {node.name for node in local.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    assert not names & owned
    assert '_read_exported_rows' in names
