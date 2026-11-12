"""Policies use concrete reply parsing without importing execution owners."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/answer-refinement-context.json')


def test_only_reply_interpretation_is_a_product_dependency():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/aggregation_pass.py')) == {'reply_json'}
    assert nonstdlib_imports(read_source(ROOT / 'scripts/refusal_pass.py')) == {'aggregation_pass'}


def test_aggregation_import_is_independent_of_model_and_retrieval_execution():
    assert_isolated_import(ROOT, 'aggregation_pass', dependencies=('reply_json',))


def test_refusal_import_is_independent_of_model_and_retrieval_execution():
    assert_isolated_import(ROOT, 'refusal_pass', dependencies=('aggregation_pass', 'reply_json'))
