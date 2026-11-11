"""An untracked copy of what the update adds does not stop it (audit 2026-09-26 A-9).

docs/research/2026-09-26-an-untracked-copy-of-the-update-does-not-stop-it.md
"""
from __future__ import annotations

import self_update

from tests.an_untracked_copy_of_the_update_does_not_stop_it_helpers import (
    NOTE,
    note_upstream,  # noqa: F401
)
from tests.self_update_helpers import (
    _commit,  # noqa: F401
    linked_clone,  # noqa: F401
)


def test_an_identical_untracked_note_is_replaced_by_the_update(note_upstream) -> None:  # noqa: F811
    (note_upstream / NOTE).write_text("# A note\n", encoding="utf-8")

    outcome = self_update.update_checkout(note_upstream)

    assert (outcome["status"], (note_upstream / NOTE).read_text(encoding="utf-8")) == ("updated", "# A note\n")


def test_an_untracked_file_that_differs_stops_the_update_by_name(note_upstream) -> None:  # noqa: F811
    (note_upstream / NOTE).write_text("# The owner's own words\n", encoding="utf-8")

    outcome = self_update.update_checkout(note_upstream)

    assert (outcome["reason"], outcome["paths"]) == ("untracked_files_conflict", [NOTE])
    assert (note_upstream / NOTE).read_text(encoding="utf-8") == "# The owner's own words\n"


def test_a_failed_merge_puts_the_copy_back_and_names_what_git_said(note_upstream, monkeypatch) -> None:  # noqa: F811
    (note_upstream / NOTE).write_text("# A note\n", encoding="utf-8")
    real_git = self_update.git

    def refusing_merge(root, *arguments, **kwargs):
        if arguments[0] == "merge":
            return real_git(root, "merge", "--ff-only", "no-such-revision")
        return real_git(root, *arguments, **kwargs)

    monkeypatch.setattr(self_update, "git", refusing_merge)

    outcome = self_update.update_checkout(note_upstream)

    assert "no-such-revision" in outcome["reason"]
    assert (note_upstream / NOTE).read_text(encoding="utf-8") == "# A note\n"
