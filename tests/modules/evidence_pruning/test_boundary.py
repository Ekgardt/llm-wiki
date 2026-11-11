"""This component can import its external libraries but no product state owner."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/evidence-pruning-context.json')


def test_external_dependencies_are_explicit_and_product_imports_absent():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/evidence_pruning.py')) == {'numpy'}
    assert_isolated_import(ROOT, 'evidence_pruning')
