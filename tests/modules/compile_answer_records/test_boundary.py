"""Saved answer validation cannot import the executable controller."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/compile-answer-records-context.json')


def test_answer_records_use_only_lower_value_contracts():
    source = ROOT / 'scripts/llm_wiki/compile_answer_records.py'
    assert nonstdlib_imports(read_source(source)) == {
        'llm_wiki.compile_event_records', 'context_budget', 'llm_wiki.model_records',
        'llm_wiki.formats.canonical',
    }


def test_import_has_no_queue_controller_or_provider_transport():
    assert_isolated_import(ROOT, 'llm_wiki.compile_answer_records', dependencies=(
        'llm_wiki', 'llm_wiki.compile_event_records', 'context_budget', 'llm_wiki.model_records',
        'llm_wiki.formats', 'llm_wiki.formats.canonical',
    ))
