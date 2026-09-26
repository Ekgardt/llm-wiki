"""An untracked copy of what the update adds does not stop it (audit 2026-09-26 A-9).

docs/research/2026-09-26-an-untracked-copy-of-the-update-does-not-stop-it.md
"""
from __future__ import annotations

import pytest
import self_update

from tests.test_self_update import _commit, linked_clone  # noqa: F401

NOTE = "docs/research/note.md"


@pytest.fixture()
def note_upstream(linked_clone, monkeypatch):  # noqa: F811
    upstream, clone = linked_clone
    (upstream / "docs" / "research").mkdir(parents=True)
    _commit(upstream, NOTE, "# A note\n")
    (clone / "docs" / "research").mkdir(parents=True)
    monkeypatch.setattr(self_update, "_synced_dependencies", lambda _root, _extras: True)
    return clone


def test_an_identical_untracked_note_is_replaced_by_the_update(note_upstream) -> None:
    (note_upstream / NOTE).write_text("# A note\n", encoding="utf-8")

    outcome = self_update.update_checkout(note_upstream)

    assert (outcome["status"], (note_upstream / NOTE).read_text(encoding="utf-8")) == ("updated", "# A note\n")


def test_an_untracked_file_that_differs_stops_the_update_by_name(note_upstream) -> None:
    (note_upstream / NOTE).write_text("# The owner's own words\n", encoding="utf-8")

    outcome = self_update.update_checkout(note_upstream)

    assert (outcome["reason"], outcome["paths"]) == ("untracked_files_conflict", [NOTE])
    assert (note_upstream / NOTE).read_text(encoding="utf-8") == "# The owner's own words\n"


def test_a_failed_merge_puts_the_copy_back_and_names_what_git_said(note_upstream, monkeypatch) -> None:
    (note_upstream / NOTE).write_text("# A note\n", encoding="utf-8")
    real_git = self_update._git

    def refusing_merge(root, *arguments, **kwargs):
        if arguments[0] == "merge":
            return real_git(root, "merge", "--ff-only", "no-such-revision")
        return real_git(root, *arguments, **kwargs)

    monkeypatch.setattr(self_update, "_git", refusing_merge)

    outcome = self_update.update_checkout(note_upstream)

    assert "no-such-revision" in outcome["reason"]
    assert (note_upstream / NOTE).read_text(encoding="utf-8") == "# A note\n"
