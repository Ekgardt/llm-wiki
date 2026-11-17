"""Pure decoding excludes operational source-authority and storage owners."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/native-event-reader-context.json')


def test_only_values_and_breadcrumb_protocol_dependencies():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/event_envelope.py')) == {
        'llm_wiki.event_envelope_values', 'breadcrumb_protocol',
    }


def test_decoder_imports_without_source_authority_or_runtime():
    assert_isolated_import(ROOT, 'event_envelope', dependencies=(
        'llm_wiki', 'llm_wiki.event_envelope_values', 'secret_redact', 'breadcrumb_protocol',
        'llm_wiki.formats', 'llm_wiki.formats.canonical', 'llm_wiki.formats.schema',
        'llm_wiki.reliability_defaults',
    ))
