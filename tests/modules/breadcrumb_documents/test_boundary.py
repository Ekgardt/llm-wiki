"""The document representation must not import storage or publication."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/breadcrumb-documents-context.json')


def test_only_protocol_byte_bound_and_canonical_dependencies():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/llm_wiki/breadcrumb_documents.py')) == {
        'breadcrumb_protocol', 'bounded_io', 'llm_wiki.formats.canonical',
    }


def test_storage_publication_and_model_imports_are_refused():
    assert_isolated_import(ROOT, 'llm_wiki.breadcrumb_documents', dependencies=(
        'llm_wiki', 'breadcrumb_protocol', 'bounded_io', 'llm_wiki.formats',
        'llm_wiki.formats.canonical', 'llm_wiki.formats.schema',
        'llm_wiki.reliability_defaults', 'llm_wiki.path_ancestry', 'llm_wiki.windows_native',
    ))
