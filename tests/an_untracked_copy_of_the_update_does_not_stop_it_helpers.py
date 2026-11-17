"""Helpers shared by test_an_untracked_copy_of_the_update_does_not_stop_it.py and the test files importing them."""
from __future__ import annotations

import pytest
import self_update

from tests.self_update_helpers import _commit

NOTE = "docs/research/note.md"


@pytest.fixture()
def note_upstream(linked_clone, monkeypatch):  # noqa: F811
    upstream, clone = linked_clone
    (upstream / "docs" / "research").mkdir(parents=True)
    _commit(upstream, NOTE, "# A note\n")
    (clone / "docs" / "research").mkdir(parents=True)
    monkeypatch.setattr(self_update, "synced_dependencies", lambda _root, _extras: True)
    return clone
