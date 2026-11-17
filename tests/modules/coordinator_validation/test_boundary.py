"""Validation cannot load the coordinator, queue, repair or registry executors."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/coordinator-validation-context.json')


def test_imports_only_declared_lower_owners():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/llm_wiki/coordinator_validation.py')) == {
        'llm_wiki.coordinator_invariants', 'llm_wiki.coordinator_schema',
        'llm_wiki.reliability_defaults', 'llm_wiki.sqlite_io',
    }
    assert_isolated_import(ROOT, 'llm_wiki.coordinator_validation', dependencies=(
        'llm_wiki', 'llm_wiki.coordinator_invariants', 'llm_wiki.coordinator_schema',
        'llm_wiki.reliability_defaults', 'llm_wiki.sqlite_io',
        'llm_wiki.sqlite_contracts', 'llm_wiki.sqlite_schema_text',
        'llm_wiki.formats', 'llm_wiki.formats.canonical',
        'llm_wiki.runtime_contracts', 'llm_wiki.runtime_environment',
        'llm_wiki.runtime_metadata', 'llm_wiki.windows_acl', 'llm_wiki.windows_native',
        'llm_wiki.sqlite_pragmas', 'llm_wiki.sqlite_deadlines',
        'llm_wiki.path_ancestry',
    ))
