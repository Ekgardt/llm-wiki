"""A module a test re-imports under a temporary vault must not outlive that test."""

import re
import sys
import types
from pathlib import Path

import pytest

from tests.forgotten_modules import forget_modules

TESTS = Path(__file__).resolve().parent
UNRESTORED_REMOVAL = re.compile(r"delitem\(\s*sys\.modules|sys\.modules\.pop\(|del sys\.modules\[")
NAME = "a_module_only_this_test_knows"


def test_a_module_first_imported_during_the_test_is_gone_afterwards():
    with pytest.MonkeyPatch.context() as monkeypatch:
        forget_modules(monkeypatch, NAME)
        sys.modules[NAME] = types.ModuleType(NAME)

    assert NAME not in sys.modules


def test_a_module_imported_before_the_test_comes_back_as_the_same_object(monkeypatch):
    original = types.ModuleType(NAME)
    monkeypatch.setitem(sys.modules, NAME, original)

    with pytest.MonkeyPatch.context() as inner:
        forget_modules(inner, NAME)
        assert NAME not in sys.modules
        sys.modules[NAME] = types.ModuleType(NAME)

    assert sys.modules[NAME] is original


def test_a_submodule_comes_back_on_its_package_as_the_same_object():
    """`from llm_wiki import part` reads the package attribute before the module table."""
    import llm_wiki
    from llm_wiki import context_bundle as original

    with pytest.MonkeyPatch.context() as monkeypatch:
        forget_modules(monkeypatch, "llm_wiki.context_bundle")
        from llm_wiki import context_bundle as reloaded

        assert reloaded is not original

    from llm_wiki import context_bundle as after

    assert (after, llm_wiki.context_bundle, sys.modules["llm_wiki.context_bundle"]) == (original, original, original)


def test_the_bare_removal_this_guards_against_really_leaks():
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.delitem(sys.modules, NAME, raising=False)
        sys.modules[NAME] = types.ModuleType(NAME)
    try:
        assert NAME in sys.modules
    finally:
        del sys.modules[NAME]


def _removes_a_module_without_an_undo(path: Path) -> bool:
    return bool(UNRESTORED_REMOVAL.search(path.read_text(encoding="utf-8")))


def test_every_test_removes_modules_through_the_helper():
    exempt = {Path(__file__).name, "forgotten_modules.py"}
    offenders = [
        path.name
        for path in TESTS.rglob("*.py")
        if "fixtures" not in path.parts and path.name not in exempt and _removes_a_module_without_an_undo(path)
    ]

    assert offenders == []
