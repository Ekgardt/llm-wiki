"""The registry can depend on data contracts only."""
from pathlib import Path

from tests.module_boundaries import nonstdlib_imports

ROOT = Path(__file__).resolve().parents[3]


def test_registry_declares_only_data_dependencies():
    source = (ROOT / 'scripts/lsp_profiles.py').read_text()
    assert nonstdlib_imports(source) == {
        'lsp_paths', 'lsp_server_profile', 'llm_wiki.pyright_configuration',
    }
