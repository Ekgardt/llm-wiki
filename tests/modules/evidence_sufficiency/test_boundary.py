"""Keep existing pure computation independent from orchestration."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports

ROOT = Path(__file__).resolve().parents[3]


def test_only_declared_computation_dependencies():
    assert nonstdlib_imports((ROOT / 'scripts/evidence_sufficiency.py').read_text()) == {'temporal_anchor'}


def test_import_does_not_load_retrieval_or_the_queue():
    assert_isolated_import(ROOT, 'evidence_sufficiency', dependencies=('temporal_anchor',))
