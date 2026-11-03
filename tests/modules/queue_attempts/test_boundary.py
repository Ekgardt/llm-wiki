"""Queue policy owners do not depend on the executor or connection lifecycle."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_executor_no_longer_defines_owned_function():
    tree = ast.parse((ROOT / 'scripts/memory_queue.py').read_text())
    assert '_apply_failure_state' not in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def test_owner_imports_only_declared_dependencies():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/queue_attempts.py').read_text())
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imports <= {'llm_wiki.reliability_defaults', 'llm_wiki.queue_timing', 'llm_wiki.queue_contracts', 'llm_wiki.formats.canonical', 'datetime', '__future__', 'collections.abc'}


def test_direct_imports_are_declared_standard_libraries():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/queue_attempts.py').read_text())
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Import)]
    assert {a.name for n in nodes for a in n.names} <= {'sqlite3'}


def test_owner_does_not_open_commit_or_close_connection():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/queue_attempts.py').read_text())
    calls = [n.func for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert not {n.attr for n in calls if isinstance(n, ast.Attribute)}.intersection(
        {'connect', 'commit', 'rollback', 'close', 'now', 'monotonic'})
