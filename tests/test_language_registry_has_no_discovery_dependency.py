"""Reading the language registry cannot require runtime discovery owners."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import

ROOT = Path(__file__).resolve().parents[1]


def test_registry_import_does_not_reach_discovery_process_or_coordination():
    assert_isolated_import(ROOT, 'lsp_profiles', dependencies=(
        'lsp_paths', 'lsp_server_profile', 'llm_wiki', 'llm_wiki.pyright_configuration',
        'llm_wiki.formats', 'llm_wiki.formats.canonical'))
