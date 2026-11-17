"""The offline stand cannot import runtime coordination or model providers."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/ledger-offline-context.json')


def test_only_declared_product_dependencies():
    assert nonstdlib_imports(read_source(ROOT / 'benchmark/ledger_offline.py')) == {
        'aggregation_pass', 'llm_wiki', 'temporal_anchor',
    }


def test_runtime_imports_are_refused():
    assert_isolated_import(
        ROOT, 'ledger_offline', source_roots=('scripts', 'benchmark'),
        dependencies=('aggregation_pass', 'llm_wiki', 'llm_wiki.ledger_core',
                      'temporal_anchor', 'reply_json'),
    )
