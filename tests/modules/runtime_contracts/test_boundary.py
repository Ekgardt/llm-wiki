"""Runtime layers must not import the former facade or higher executors."""
import ast
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports

ROOT = Path(__file__).resolve().parents[3]


def test_former_facade_has_no_second_implementation():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text())
    assert 'UnsafeStateRoot' not in {getattr(n, 'name', None) for n in tree.body}


def test_owner_has_only_declared_lower_dependencies():
    source = (ROOT / 'scripts/llm_wiki/runtime_contracts.py').read_text()
    assert nonstdlib_imports(source) <= set()


def test_import_never_reaches_the_facade_or_executor():
    assert_isolated_import(ROOT, 'llm_wiki.runtime_contracts', dependencies=('llm_wiki',))
