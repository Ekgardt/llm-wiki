"""Value codecs cannot import discovery or runtime ownership."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/lsp-install-manifest-context.json')


def test_only_profile_and_canonical_values_are_dependencies():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/llm_wiki/lsp_install_manifest.py')) == {
        'lsp_server_profile', 'llm_wiki.formats.canonical',
    }


def test_import_has_no_discovery_or_io_dependency():
    assert_isolated_import(ROOT, 'llm_wiki.lsp_install_manifest', dependencies=(
        'llm_wiki', 'llm_wiki.formats', 'llm_wiki.formats.canonical', 'lsp_server_profile',
    ))
