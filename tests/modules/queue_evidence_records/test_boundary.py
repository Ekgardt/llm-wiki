"""Representation depends downward and never owns queue execution or SQL."""
import ast
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/queue-evidence-records-context.json')
SOURCE = ROOT / 'scripts/llm_wiki/queue_evidence_records.py'


def test_representation_has_only_the_existing_lower_contracts():
    assert nonstdlib_imports(read_source(SOURCE)) == {
        'llm_wiki.queue_contracts', 'llm_wiki.queue_timing', 'llm_wiki.formats.schema',
        'llm_wiki.formats.canonical',
    }


def test_representation_import_cannot_load_the_queue():
    assert_isolated_import(ROOT, 'llm_wiki.queue_evidence_records', dependencies=(
        'llm_wiki', 'llm_wiki.queue_contracts', 'llm_wiki.queue_timing',
        'llm_wiki.formats', 'llm_wiki.formats.schema', 'llm_wiki.formats.canonical',
        'llm_wiki.reliability_defaults',
    ))


def test_sql_execution_stays_outside_the_representation_owner():
    tree = ast.parse(read_source(SOURCE))
    attributes = {node.func.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not attributes & {'connect', 'execute', 'executemany', 'commit', 'rollback'}
