"""Consumers read immutable defaults from their owner, without durability imports."""
import ast
import sys
from pathlib import Path

from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/reliability-defaults-context.json')


def _import_roots(node):
    if isinstance(node, ast.ImportFrom):
        assert node.level == 0
        return {node.module.split('.')[0]}
    if isinstance(node, ast.Import):
        return {alias.name.split('.')[0] for alias in node.names}
    return set()

def test_contract_source_imports_only_standard_library():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/reliability_defaults.py', encoding='utf-8'))
    roots = set().union(*(_import_roots(node) for node in ast.walk(tree)))
    assert roots <= sys.stdlib_module_names
