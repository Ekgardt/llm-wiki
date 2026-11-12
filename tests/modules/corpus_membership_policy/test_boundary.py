"""Immutable membership policy never loads corpus collection or runtime owners."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/corpus-membership-policy-context.json')


def test_policy_has_no_product_runtime_dependencies():
    assert not nonstdlib_imports(read_source(ROOT / 'scripts/llm_wiki/corpus_membership_policy.py'))


def test_policy_import_is_isolated_from_collectors():
    assert_isolated_import(ROOT, 'llm_wiki.corpus_membership_policy', dependencies=('llm_wiki',))
