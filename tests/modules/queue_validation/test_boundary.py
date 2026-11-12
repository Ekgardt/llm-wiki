"""Queue policy owners do not depend on the executor or connection lifecycle."""
import ast
from pathlib import Path

from tests.module_boundaries import assert_isolated_import
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/queue-validation-context.json')


def test_owner_imports_only_declared_dependencies():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/queue_validation.py'))
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imports <= {'collections.abc', '__future__', 'typing', 'llm_wiki.queue_contracts',
                       'llm_wiki.formats.canonical', 'llm_wiki.formats.queue_payload',
                       'llm_wiki.restricted_paths'}


def test_direct_imports_are_declared_standard_libraries():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/queue_validation.py'))
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Import)]
    assert {a.name for n in nodes for a in n.names} <= {'re', 'sqlite3'}


def test_owner_does_not_open_commit_or_close_connection():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/queue_validation.py'))
    calls = [n.func for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert not {n.attr for n in calls if isinstance(n, ast.Attribute)}.intersection(
        {'connect', 'commit', 'rollback', 'close', 'now', 'monotonic'})


def test_import_cannot_reach_queue_execution_or_runtime_owners():
    assert_isolated_import(ROOT, 'llm_wiki.queue_validation', dependencies=(
        'llm_wiki', 'llm_wiki.queue_contracts', 'llm_wiki.formats',
        'llm_wiki.formats.canonical', 'llm_wiki.formats.queue_payload',
        'llm_wiki.restricted_paths'))
