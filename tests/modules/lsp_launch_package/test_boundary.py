"""The tree builder does not own process/session lifecycle."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/lsp-launch-package-context.json')

def test_dependencies_are_readers_and_profile_values():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/lsp_launch_package.py')) == {'bounded_io', 'lsp_server_profile'}

def test_import_excludes_process_and_runtime_owners():
    assert_isolated_import(ROOT, 'lsp_launch_package', dependencies=('bounded_io', 'lsp_server_profile', 'llm_wiki', 'llm_wiki.path_ancestry'))
