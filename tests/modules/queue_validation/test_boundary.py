"""Queue policy owners do not depend on the executor or connection lifecycle."""
import ast
from pathlib import Path

from tests.module_boundaries import assert_isolated_import

ROOT = Path(__file__).resolve().parents[3]


def _defined_functions(path):
    return {n.name for n in ast.parse(path.read_text()).body if isinstance(n, ast.FunctionDef)}


def test_executor_no_longer_defines_owned_function():
    owned = _defined_functions(ROOT / 'scripts/llm_wiki/queue_validation.py')
    executor = _defined_functions(ROOT / 'scripts/memory_queue.py')
    assert not owned.intersection(executor)


def test_owner_imports_only_declared_dependencies():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/queue_validation.py').read_text())
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imports <= {'collections.abc', '__future__', 'typing', 'llm_wiki.queue_contracts',
                       'llm_wiki.formats.canonical', 'llm_wiki.formats.queue_payload',
                       'llm_wiki.restricted_paths'}


def test_direct_imports_are_declared_standard_libraries():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/queue_validation.py').read_text())
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Import)]
    assert {a.name for n in nodes for a in n.names} <= {'re', 'sqlite3'}


def test_owner_does_not_open_commit_or_close_connection():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/queue_validation.py').read_text())
    calls = [n.func for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert not {n.attr for n in calls if isinstance(n, ast.Attribute)}.intersection(
        {'connect', 'commit', 'rollback', 'close', 'now', 'monotonic'})


def test_import_cannot_reach_queue_execution_or_runtime_owners():
    assert_isolated_import(ROOT, 'llm_wiki.queue_validation', dependencies=(
        'llm_wiki', 'llm_wiki.queue_contracts', 'llm_wiki.formats',
        'llm_wiki.formats.canonical', 'llm_wiki.formats.queue_payload',
        'llm_wiki.restricted_paths'))
