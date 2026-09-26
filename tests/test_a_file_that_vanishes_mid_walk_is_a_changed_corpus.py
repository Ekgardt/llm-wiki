"""A name the listing gave and the open cannot find is the tree moving, not a bound.

Research:
`docs/research/2026-09-18-a-file-that-vanishes-mid-walk-is-a-changed-corpus.md`.
"""
from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import pytest

from tests.slow_machine import SHORT_TIMEOUT

TESTS = Path(__file__).resolve().parent
for directory in (TESTS.parent / "scripts", TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))


def _discovery(vault: Path):
    import corpus_snapshot

    return corpus_snapshot._Discovery(
        vault,
        max_files=100,
        max_entries=1000,
        max_directories=100,
        max_depth=8,
        max_file_bytes=1 << 20,
        max_total_bytes=1 << 22,
        deadline=time.monotonic() + SHORT_TIMEOUT,
        include_archives=False,
    )


def _notes(tmp_path: Path) -> Path:
    notes = tmp_path / "knowledge/notes"
    notes.mkdir(parents=True)
    return notes


@pytest.mark.skipif(os.name != "posix", reason="the descriptor walk is the POSIX one")
def test_a_source_the_listing_named_and_the_open_cannot_find_is_a_changed_corpus(
    tmp_path,
):
    import corpus_snapshot

    notes = _notes(tmp_path)
    page = notes / "page.md"
    page.write_text("# page\n", encoding="utf-8")
    info = page.lstat()
    page.unlink()
    descriptor = os.open(notes, os.O_RDONLY | os.O_DIRECTORY)

    try:
        with pytest.raises(corpus_snapshot.CorpusChanged):
            _discovery(tmp_path)._posix_source(
                notes, page, descriptor, "note", "page.md", info
            )
    finally:
        os.close(descriptor)


@pytest.mark.skipif(os.name != "posix", reason="the descriptor walk is the POSIX one")
def test_a_directory_the_listing_named_and_the_open_cannot_find_is_a_changed_corpus(
    tmp_path,
):
    import corpus_snapshot

    notes = _notes(tmp_path)
    child = notes / "child"
    child.mkdir()
    info = child.lstat()
    child.rmdir()
    descriptor = os.open(notes, os.O_RDONLY | os.O_DIRECTORY)

    try:
        with pytest.raises(corpus_snapshot.CorpusChanged):
            _discovery(tmp_path)._posix_child(
                notes, child, 1, descriptor, "note", "child", info
            )
    finally:
        os.close(descriptor)


def test_an_entry_that_goes_between_the_listing_and_its_metadata_is_a_changed_corpus(
    tmp_path,
):
    """The Windows walk reads each listed entry's metadata after the listing."""
    import corpus_snapshot

    notes = _notes(tmp_path)
    page = notes / "page.md"
    page.write_text("# page\n", encoding="utf-8")
    with os.scandir(notes) as listing:
        entry = next(iter(listing))
    page.unlink()

    with pytest.raises(corpus_snapshot.CorpusChanged):
        _discovery(tmp_path)._entry_info(entry)


def test_an_entry_that_is_still_there_is_read_normally(tmp_path):
    notes = _notes(tmp_path)
    (notes / "page.md").write_text("# page\n", encoding="utf-8")
    with os.scandir(notes) as listing:
        entry = next(iter(listing))

    info = _discovery(tmp_path)._entry_info(entry)

    assert stat.S_ISREG(info.st_mode)
