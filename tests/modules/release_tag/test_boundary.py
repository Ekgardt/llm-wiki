"""Tag orchestration has one concrete full-source local dependency."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/release-tag-context.json')


def test_release_tag_depends_only_on_release_manifest():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/release_tag.py')) == {'release_manifest'}


def test_import_does_not_need_product_runtime():
    assert_isolated_import(ROOT, 'release_tag', dependencies=('release_manifest',))
