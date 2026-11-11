"""Forget imported modules for one test and put the module table back afterwards."""

import importlib
import sys

import pytest


def forget_modules(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """Make the next import of each name run the module again, for this test only.

    ``monkeypatch.delitem(sys.modules, name, raising=False)`` records nothing for a
    module not yet imported, so the copy the test then imports, bound to its temporary
    vault, would outlive it. Setting the entry first gives the undo a record either way.
    A submodule is also an attribute of its package, and ``from package import name``
    reads that attribute before the module table, so it is forgotten there as well.
    """
    for name in names:
        monkeypatch.setitem(sys.modules, name, None)
        monkeypatch.delitem(sys.modules, name)
        _forget_package_attribute(monkeypatch, name)


def _forget_package_attribute(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    package_name, _, attribute = name.rpartition('.')
    if not package_name:
        return
    package = importlib.import_module(package_name)
    monkeypatch.setattr(package, attribute, None, raising=False)
    monkeypatch.delattr(package, attribute)
