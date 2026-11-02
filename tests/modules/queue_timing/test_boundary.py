"""Timing has one owner, without an executor dependency."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
NAMES = {'_as_utc', '_no_retry_after', '_retry_after_number', '_bounded_retry_until', '_retry_after_deadline', '_retry_after_from_text', '_bounded_retry_number', '_retry_available_at', '_timestamp', '_retry_after_text', '_parse_timestamp', '_retry_after_seconds'}


def test_executor_does_not_define_timing_functions():
    tree = ast.parse((ROOT / 'scripts/memory_queue.py').read_text())
    names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert not names.intersection(NAMES)


def test_owner_imports_only_declared_dependencies():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/queue_timing.py').read_text())
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imports <= {'__future__', 'datetime', 'llm_wiki.reliability_defaults'}


def test_direct_imports_are_stdlib():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/queue_timing.py').read_text())
    direct = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert direct <= {'email.utils', 'math', 'random'}
