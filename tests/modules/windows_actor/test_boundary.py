"""The native actor probe imports no state, registry or execution module."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/windows-actor-context.json')


def test_native_probe_has_only_stdlib_dependencies():
    assert not nonstdlib_imports(read_source(ROOT / 'scripts/llm_wiki/windows_actor.py'))
    assert_isolated_import(ROOT, 'llm_wiki.windows_actor', dependencies=('llm_wiki',))
