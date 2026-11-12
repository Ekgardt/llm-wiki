"""Helpers shared by test_symbol_registry_cancellation.py and the test files importing them."""
from __future__ import annotations

import import_resolver


class _CancelAfter:
    """A ``cancelled`` callable that flips once the walk has parsed enough."""

    def __init__(self, monkeypatch, limit: int) -> None:
        self.parsed = 0
        self._limit = limit
        real_parse = import_resolver.ast.parse

        def counting_parse(*args, **kwargs):
            self.parsed += 1
            return real_parse(*args, **kwargs)

        monkeypatch.setattr(import_resolver.ast, "parse", counting_parse)

    def __call__(self) -> bool:
        return self.parsed >= self._limit
