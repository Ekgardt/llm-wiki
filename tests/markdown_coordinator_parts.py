"""Stand-ins reach every part of the composed Markdown coordinator that looks a name up."""
from __future__ import annotations

import sys
from types import ModuleType

import markdown_transaction
import pytest
from llm_wiki import markdown_store

PARTS_SPLIT_FROM_THE_FACADE = (
    'markdown_transaction',
    'llm_wiki.markdown_admission',
    'llm_wiki.markdown_append',
    'llm_wiki.markdown_migration',
    'llm_wiki.markdown_mutation',
)


def coordinator_modules() -> list[ModuleType]:
    names = {part.__module__ for part in markdown_transaction.MarkdownCoordinator.__mro__}
    names.update(PARTS_SPLIT_FROM_THE_FACADE)
    parts = [sys.modules[name] for name in sorted(names) if name != 'builtins']
    return [markdown_store, *parts]


def binding_modules(name: str) -> list[ModuleType]:
    found = [module for module in coordinator_modules() if name in vars(module)]
    assert found, f'no part of the Markdown coordinator binds {name}'
    return found


def patch_coordinator(monkeypatch: pytest.MonkeyPatch, name: str, value: object) -> None:
    for module in binding_modules(name):
        monkeypatch.setattr(module, name, value)
