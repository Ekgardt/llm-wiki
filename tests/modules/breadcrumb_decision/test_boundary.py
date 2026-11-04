"""Receipt construction requires no storage, provider or queue module."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/breadcrumb-decision-context.json')


def test_only_declared_receipt_dependencies():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/breadcrumb_decision.py')) == {
        'breadcrumb_protocol', 'llm_wiki', 'llm_wiki.formats.canonical',
        'llm_wiki.reliability_defaults',
    }


def test_import_refuses_runtime_and_provider_dependencies():
    assert_isolated_import(ROOT, 'breadcrumb_decision', dependencies=(
        'breadcrumb_protocol', 'llm_wiki', 'llm_wiki.breadcrumb_documents', 'bounded_io',
        'llm_wiki.formats', 'llm_wiki.formats.canonical', 'llm_wiki.formats.schema',
        'llm_wiki.reliability_defaults', 'llm_wiki.path_ancestry', 'llm_wiki.windows_native',
    ))
