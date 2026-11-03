"""The pure source contract must not be owned by the queue executor."""
import ast
from pathlib import Path

from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/queue-sources-context.json')


def test_source_contract_imports_only_stdlib():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/queue_sources.py'))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported <= {'__future__', 'datetime', 'pathlib', 'types', 'llm_wiki.sqlite_contracts'}


def test_direct_imports_are_stdlib():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/queue_sources.py'))
    direct = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
              for alias in node.names}
    assert direct <= {'json', 're'}
