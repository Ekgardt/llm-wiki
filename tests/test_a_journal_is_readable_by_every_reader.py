"""A page the project journal accepts is one every reader of it accepts.

Four bounds met the same file, `knowledge/projects/<slug>/journal.md`, and
two of them were smaller than what the journal itself allows. A 4.2 MB
journal was refused by the claim index for three days after the claim
tree's cap had been raised (2026-09-10). See
`docs/research/2026-09-10-one-ceiling-for-every-reader-of-a-journal.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import claim_tree_manifest  # noqa: E402
import claims  # noqa: E402
import lint_memory  # noqa: E402
import project_journal  # noqa: E402


def test_every_reader_of_a_journal_accepts_what_the_journal_may_be() -> None:
    ceiling = project_journal.MAX_JOURNAL_BYTES
    readers = {
        "claim tree": claim_tree_manifest.MAX_CLAIM_TREE_FILE_BYTES,
        "claim index": claims.MAX_CLAIM_PAGE_BYTES,
        "lint": lint_memory.MAX_LINT_PAGE_BYTES,
    }
    too_small = {name: cap for name, cap in readers.items() if cap < ceiling}
    assert too_small == {}, f"readers below the journal ceiling {ceiling}: {too_small}"


def test_the_journal_names_every_file_the_readers_scan() -> None:
    assert claim_tree_manifest.PROJECT_CLAIM_FILES == {"context.md", "journal.md", "state.md"}
    assert "journal.md" in claim_tree_manifest.PROJECT_CLAIM_FILES
