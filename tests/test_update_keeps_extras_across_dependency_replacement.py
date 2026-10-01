"""An extra's old runtime identifies the choice before its dependency changes."""
from __future__ import annotations

import self_update

from tests import test_self_update as git_fixtures

linked_clone = git_fixtures.linked_clone


def _project(runtime: str) -> str:
    return f'[project]\nname = "llm-wiki"\n[project.optional-dependencies]\nsemantic = ["{runtime}"]\n'


def test_runtime_replacement_preserves_the_selected_extra(linked_clone, monkeypatch) -> None:
    upstream, clone = linked_clone
    git_fixtures._commit(upstream, "pyproject.toml", _project("sentence-transformers"))
    git_fixtures._git(clone, "pull", "--ff-only")
    git_fixtures._commit(upstream, "pyproject.toml", _project("onnxruntime"))
    monkeypatch.setattr(self_update, "_installed_distributions", lambda: {"sentence-transformers": set()})
    observed = []
    monkeypatch.setattr(self_update, "_synced_dependencies", lambda _root, extras: observed.append(extras) is None)

    outcome = self_update.update_checkout(clone)

    assert outcome["status"] == "updated"
    assert observed == [("semantic",)]
    assert outcome["extras"] == ("semantic",)
