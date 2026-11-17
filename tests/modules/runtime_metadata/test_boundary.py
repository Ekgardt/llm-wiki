"""Metadata validation cannot pull in byte/identity/Windows-workspace execution."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/runtime-metadata-context.json')


def test_metadata_has_only_environment_dependencies():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/llm_wiki/runtime_metadata.py')) == {
        'llm_wiki.runtime_environment',
    }
    assert_isolated_import(ROOT, 'llm_wiki.runtime_metadata', dependencies=(
        'llm_wiki', 'llm_wiki.runtime_environment', 'llm_wiki.runtime_contracts',
        'llm_wiki.windows_acl', 'llm_wiki.windows_native',
    ))
