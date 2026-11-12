"""Event construction does not load physical records or runtime owners."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/event-envelope-values-context.json')


def test_only_redaction_dependency():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/llm_wiki/event_envelope_values.py')) == {'secret_redact'}


def test_value_owner_imports_without_transport_or_runtime():
    assert_isolated_import(ROOT, 'llm_wiki.event_envelope_values', dependencies=('llm_wiki', 'secret_redact'))
