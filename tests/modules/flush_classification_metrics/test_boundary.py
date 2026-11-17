"""Scoring loads only the shared protocol and standard library."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/flush-classification-metrics-context.json')


def test_only_protocol_dependency():
    assert nonstdlib_imports(read_source(ROOT / 'benchmark/flush_classification_metrics.py')) == {
        'llm_wiki.classification_protocol',
    }


def test_import_has_no_provider_or_runtime_dependency():
    assert_isolated_import(
        ROOT, 'flush_classification_metrics',
        dependencies=('llm_wiki', 'llm_wiki.classification_protocol'),
        source_roots=('benchmark', 'scripts'),
    )
