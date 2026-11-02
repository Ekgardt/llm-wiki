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
