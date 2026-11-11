"""Timing has one owner, without an executor dependency."""
import ast
from pathlib import Path

from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/queue-timing-context.json')


def test_owner_imports_only_declared_dependencies():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/queue_timing.py'))
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imports <= {'__future__', 'datetime', 'llm_wiki.reliability_defaults'}


def test_direct_imports_are_stdlib():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/queue_timing.py'))
    direct = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert direct <= {'email.utils', 'math', 'random'}
