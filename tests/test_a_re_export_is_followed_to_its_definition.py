"""A call through a package re-export reaches the definition (audit 2026-09-26 B-6).

docs/research/2026-09-26-a-re-export-is-followed-to-its-definition.md
"""
from __future__ import annotations

from code_extractor import extract_code

from tests.test_a_decorator_is_a_call_until_it_is_a_route import _call_target_names, _source

FILES = {
    "lib/__init__.py": b"from .core import compute\n",
    "lib/core.py": b"def compute():\n    return 1\n",
    "app.py": b"from lib import compute as calc\n\n\ndef run():\n    return calc()\n",
}


def test_an_aliased_call_through_the_package_reaches_the_definition() -> None:
    result = extract_code(tuple(_source(path, content) for path, content in FILES.items()), repository_id="repo")

    assert _call_target_names(result) == ["compute"]


def test_a_re_export_cycle_resolves_to_nothing_and_ends() -> None:
    files = {"a/__init__.py": b"from b import thing\n", "b/__init__.py": b"from a import thing\n", "m.py": b"from a import thing\n\n\ndef f():\n    return thing()\n"}

    result = extract_code(tuple(_source(path, content) for path, content in files.items()), repository_id="repo")

    assert _call_target_names(result) == []
