"""Queue policy owners do not depend on the executor or connection lifecycle."""
import ast
from pathlib import Path

from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/queue-attempts-context.json')


def test_owner_imports_only_declared_dependencies():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/queue_attempts.py'))
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imports <= {'llm_wiki.reliability_defaults', 'llm_wiki.queue_timing', 'llm_wiki.queue_contracts', 'llm_wiki.formats.canonical', 'datetime', '__future__', 'collections.abc'}


def test_direct_imports_are_declared_standard_libraries():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/queue_attempts.py'))
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Import)]
    assert {a.name for n in nodes for a in n.names} <= {'sqlite3'}


def test_owner_does_not_open_commit_or_close_connection():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/queue_attempts.py'))
    calls = [n.func for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert not {n.attr for n in calls if isinstance(n, ast.Attribute)}.intersection(
        {'connect', 'commit', 'rollback', 'close', 'now', 'monotonic'})
