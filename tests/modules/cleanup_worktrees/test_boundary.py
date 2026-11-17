"""The existing CLI depends only on the shared Git policy owner."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/cleanup-worktrees-context.json')


def test_only_repository_scope_is_a_product_dependency():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/cleanup_worktrees.py')) == {'repository_scope'}


def test_import_cannot_reach_runtime_or_installation_state():
    assert_isolated_import(ROOT, 'cleanup_worktrees', dependencies=('repository_scope',))
