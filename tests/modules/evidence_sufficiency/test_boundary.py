"""Keep existing pure computation independent from orchestration."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/evidence-sufficiency-context.json')


def test_only_declared_computation_dependencies():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/evidence_sufficiency.py')) == {'temporal_anchor'}


def test_import_does_not_load_retrieval_or_the_queue():
    assert_isolated_import(ROOT, 'evidence_sufficiency', dependencies=('temporal_anchor',))
