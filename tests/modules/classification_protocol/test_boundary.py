"""Protocol construction and parsing require no runtime or provider imports."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/classification-protocol-context.json')


def test_protocol_has_no_nonstdlib_imports():
    assert not nonstdlib_imports(read_source(ROOT / 'scripts/llm_wiki/classification_protocol.py'))


def test_protocol_import_is_isolated():
    assert_isolated_import(ROOT, 'llm_wiki.classification_protocol', dependencies=('llm_wiki',))
