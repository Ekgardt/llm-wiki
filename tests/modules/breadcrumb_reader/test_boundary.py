"""The reader imports only the complete document and stable IO owners."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/breadcrumb-reader-context.json')


def test_reader_dependencies():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/llm_wiki/breadcrumb_reader.py')) == {
        'bounded_io', 'llm_wiki.breadcrumb_documents',
    }


def test_reader_imports_without_publication_or_runtime():
    assert_isolated_import(ROOT, 'llm_wiki.breadcrumb_reader', dependencies=(
        'llm_wiki', 'llm_wiki.breadcrumb_documents', 'breadcrumb_protocol', 'bounded_io',
        'llm_wiki.formats', 'llm_wiki.formats.canonical', 'llm_wiki.formats.schema',
        'llm_wiki.reliability_defaults', 'llm_wiki.path_ancestry', 'llm_wiki.windows_native',
    ))
