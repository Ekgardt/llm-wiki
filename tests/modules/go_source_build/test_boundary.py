"""Staged compiler execution depends on profile values, never the installer."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/go-source-build-context.json')


def test_only_lsp_server_profiler_is_imported():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/go_source_build.py')) == {'lsp_server_profile'}
    assert_isolated_import(ROOT, 'go_source_build', dependencies=('lsp_server_profile',))
