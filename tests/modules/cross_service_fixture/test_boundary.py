"""Fixture creation has no runtime or provider import dependency."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/cross-service-fixture-context.json')


def test_only_standard_library_dependencies():
    assert not nonstdlib_imports(read_source(ROOT / 'benchmark/build_cross_service_fixture.py'))


def test_no_runtime_imports():
    assert_isolated_import(ROOT, 'build_cross_service_fixture', source_roots=('benchmark', 'scripts'))
