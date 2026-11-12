"""Unreadable process metadata never proves an empty process group."""
from pathlib import Path

import lsp_process_tree


class _Entry:
    """A /proc entry, named like a process, whose `stat` cannot be read."""

    def __init__(self, name: str, path: str) -> None:
        self.name = name
        self.path = path


def test_a_proc_entry_that_cannot_be_read_leaves_the_group_unknown(
    tmp_path: Path,
) -> None:
    unreadable = tmp_path / "4242"
    unreadable.mkdir()
    (unreadable / "stat").mkdir()  # a directory where the file belongs: EISDIR
    entries = [_Entry("4242", str(unreadable))]

    verdict = lsp_process_tree.scan_proc_entries(entries, 4242)

    assert verdict is None


def test_an_entry_proven_outside_the_group_is_passed_over(tmp_path: Path) -> None:
    gone = tmp_path / "4243"
    gone.mkdir()
    entries = [_Entry("4243", str(gone))]

    verdict = lsp_process_tree.scan_proc_entries(entries, 4243)

    assert verdict is True
