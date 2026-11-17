"""A leaf boundary guard detects local, nested and relative dependencies."""
import pytest

from tests.module_boundaries import nonstdlib_imports


@pytest.mark.parametrize('source', [
    'import memory_queue', 'from .json import loads', 'from . import local',
    'def f():\n    from reliable_memory import begin_immediate',
    'from llm_wiki import sqlite_contracts',
])
def test_nonstdlib_dependency_is_refused(source):
    assert nonstdlib_imports(source)


@pytest.mark.parametrize('source', ['import json as j', 'from datetime import date', 'from __future__ import annotations'])
def test_stdlib_imports_are_accepted(source):
    assert not nonstdlib_imports(source)


def test_declared_dependency_does_not_allow_another_product_module(tmp_path):
    from tests.module_boundaries import assert_isolated_import

    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    (scripts / 'owner.py').write_text('import dependency\n')
    (scripts / 'dependency.py').write_text('import forbidden\n')
    (scripts / 'forbidden.py').write_text('')
    with pytest.raises(AssertionError, match='forbidden'):
        assert_isolated_import(tmp_path, 'owner', dependencies=('dependency',))


def test_explicit_dependency_is_allowed_without_weakening_default(tmp_path):
    from tests.module_boundaries import assert_isolated_import

    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    (scripts / 'owner.py').write_text('import dependency\n')
    (scripts / 'dependency.py').write_text('')
    with pytest.raises(AssertionError, match='dependency'):
        assert_isolated_import(tmp_path, 'owner')
    assert_isolated_import(tmp_path, 'owner', dependencies=('dependency',))


@pytest.mark.parametrize('forbidden_root', ['scripts', 'benchmark'])
def test_explicit_launch_roots_guard_dependencies_in_each_root(tmp_path, forbidden_root):
    from tests.module_boundaries import assert_isolated_import

    scripts, benchmark = tmp_path / 'scripts', tmp_path / 'benchmark'
    scripts.mkdir()
    benchmark.mkdir()
    (benchmark / 'owner.py').write_text('import dependency\n')
    (scripts / 'dependency.py').write_text('import forbidden\n')
    (tmp_path / forbidden_root / 'forbidden.py').write_text('')
    with pytest.raises(AssertionError, match='forbidden'):
        assert_isolated_import(tmp_path, 'owner', dependencies=('dependency',),
                               source_roots=('scripts', 'benchmark'))
    (scripts / 'dependency.py').write_text('')
    assert_isolated_import(tmp_path, 'owner', dependencies=('dependency',),
                           source_roots=('scripts', 'benchmark'))
