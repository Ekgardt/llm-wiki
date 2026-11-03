"""Static defaults cannot import discovery or process owners."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports

ROOT = Path(__file__).resolve().parents[3]


def test_defaults_use_only_canonical_identity():
    source = (ROOT / 'scripts/llm_wiki/pyright_configuration.py').read_text()
    assert nonstdlib_imports(source) == {'llm_wiki.formats.canonical'}


def test_defaults_import_without_runtime_discovery():
    assert_isolated_import(ROOT, 'llm_wiki.pyright_configuration', dependencies=(
        'llm_wiki', 'llm_wiki.formats', 'llm_wiki.formats.canonical'))
