"""Model values must load without the provider transport or compile controller."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports

ROOT = Path(__file__).resolve().parents[3]


def test_values_depend_only_on_existing_lower_value_contracts():
    source = ROOT / 'scripts/llm_wiki/model_records.py'
    assert nonstdlib_imports(source.read_text()) == {
        'context_budget', 'llm_wiki.formats.canonical',
    }


def test_values_do_not_import_transport_or_controller():
    assert_isolated_import(ROOT, 'llm_wiki.model_records', dependencies=(
        'llm_wiki', 'context_budget', 'llm_wiki.formats', 'llm_wiki.formats.canonical',
    ))
