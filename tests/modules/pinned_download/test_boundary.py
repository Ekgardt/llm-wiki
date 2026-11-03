"""Pinned transfers import no installer or runtime executor."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/pinned-download-context.json')


def test_only_byte_bound_owner_is_imported():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/pinned_download.py')) == {'bounded_io'}
    assert_isolated_import(ROOT, 'pinned_download', dependencies=(
        'bounded_io', 'llm_wiki', 'llm_wiki.path_ancestry',
    ))
