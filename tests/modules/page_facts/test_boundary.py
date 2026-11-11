"""The reader depends on policies, never on its MCP consumer or corpus collector."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/page-facts-context.json')


def test_only_the_three_concrete_policy_owners_are_imported():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/page_facts.py')) == {
        'okf_types', 'page_status', 'provenance',
    }


def test_the_reader_import_does_not_load_consumers_or_state_owners():
    assert_isolated_import(ROOT, 'page_facts', dependencies=(
        'okf_types', 'page_status', 'provenance', 'llm_wiki',
        'llm_wiki.corpus_membership_policy',
    ))
