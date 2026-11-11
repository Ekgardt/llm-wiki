"""The registry can depend on data contracts only."""
from pathlib import Path

from tests.module_boundaries import nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/lsp-profiles-context.json')


def test_registry_declares_only_data_dependencies():
    source = read_source(ROOT / 'scripts/lsp_profiles.py')
    assert nonstdlib_imports(source) == {
        'lsp_paths', 'lsp_server_profile', 'llm_wiki.pyright_configuration',
    }
