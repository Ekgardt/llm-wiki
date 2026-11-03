"""Protocol values and the exporter cannot import benchmark execution."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/longmemeval-protocol-context.json')


def test_protocol_only_uses_stdlib():
    assert not nonstdlib_imports(read_source(ROOT / 'benchmark/longmemeval_official.py'))


def test_exporter_only_uses_protocol():
    assert nonstdlib_imports(read_source(ROOT / 'benchmark/longmemeval_hypotheses.py')) == {'longmemeval_official'}


def test_executor_imports_are_refused():
    assert_isolated_import(ROOT, 'longmemeval_hypotheses', dependencies=('longmemeval_official',), source_roots=('scripts', 'benchmark'))
