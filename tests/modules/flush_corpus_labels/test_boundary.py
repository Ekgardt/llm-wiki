"""Label interpretation has no runtime or provider import dependency."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/flush-corpus-labels-context.json')


def test_only_standard_library_dependencies():
    assert not nonstdlib_imports(read_source(ROOT / 'benchmark/flush_corpus_labels.py'))


def test_no_runtime_imports():
    assert_isolated_import(ROOT, 'flush_corpus_labels', source_roots=('benchmark', 'scripts'))
