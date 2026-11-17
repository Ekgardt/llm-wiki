"""Values import no registry, coordinator or filesystem executor."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/ownership-contracts-context.json')


def test_only_value_dependencies():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/llm_wiki/ownership_contracts.py')) == {'llm_wiki.runtime_contracts'}
    assert_isolated_import(ROOT, 'llm_wiki.ownership_contracts', dependencies=('llm_wiki', 'llm_wiki.runtime_contracts'))
