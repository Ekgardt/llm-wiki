"""Physical record syntax must not resolve or import a live runtime."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/breadcrumb-protocol-context.json')


def test_only_formats_and_policy_imports():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/breadcrumb_protocol.py')) == {
        'llm_wiki.formats.canonical', 'llm_wiki.formats.schema', 'llm_wiki.reliability_defaults',
    }


def test_import_cannot_load_state_queue_or_runtime_facades():
    assert_isolated_import(ROOT, 'breadcrumb_protocol', dependencies=(
        'llm_wiki', 'llm_wiki.formats', 'llm_wiki.formats.canonical',
        'llm_wiki.formats.schema', 'llm_wiki.reliability_defaults',
    ))
