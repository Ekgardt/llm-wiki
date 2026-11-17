"""Pure metrics cannot reach the runner, storage or provider implementations."""
import ast
import hashlib
import json
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/selective-forgetting-metrics-context.json')


def test_imports_only_standard_library():
    assert nonstdlib_imports(read_source(ROOT / 'benchmark/selective_forgetting_metrics.py')) == set()
    assert_isolated_import(ROOT, 'selective_forgetting_metrics', source_roots=('scripts', 'benchmark'))


def _definition_name(node):
    if isinstance(node, ast.FunctionDef):
        return node.name
    if isinstance(node, ast.Assign):
        return node.targets[0].id
    return None


def test_original_calculation_and_dispatch_asts_are_preserved():
    contract = json.loads(read_source(ROOT / 'tests/modules/selective_forgetting_metrics/original-contract.json'))
    tree = ast.parse(read_source(ROOT / 'benchmark/selective_forgetting_metrics.py'))
    actual = {_definition_name(node): hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
              for node in tree.body if _definition_name(node) is not None}
    assert actual == contract['definitions']
