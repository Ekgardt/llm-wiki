"""The corpus generator cannot load navigation or runtime executors."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/python-qualification-corpus-context.json')


def test_generator_has_only_stdlib_imports():
    assert not nonstdlib_imports(read_source(ROOT / 'benchmark/generate_python_qualification.py'))


def test_navigation_and_runtime_imports_are_refused():
    assert_isolated_import(ROOT, 'generate_python_qualification', source_roots=('scripts', 'benchmark'))
