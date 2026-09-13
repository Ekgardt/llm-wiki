"""A question about one name reads only the sources that mention it.

`find_dead_code(symbol=...)` used to parse and walk every Python source in the
repository to answer about one function — 551 files and 11 s of a single answer
on the installed vault, measured 2026-09-12. Narrowing is sound because a name
absent from a file's bytes cannot be loaded in its syntax tree, and the answer
says how many sources it skipped. Research:
`docs/research/2026-09-12-sixteen-of-sixteen.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import value_references  # noqa: E402

MENTIONS = b"""\
from helpers import wanted


def caller():
    return wanted
"""

SILENT = b"""\
def unrelated():
    return 1
"""


def _sources() -> list[tuple[str, bytes]]:
    return [("pkg/mentions.py", MENTIONS), ("pkg/silent.py", SILENT)]


def test_a_narrowed_index_reads_only_the_sources_that_mention_the_name():
    index = value_references.build_reference_index(
        _sources(), names=frozenset({"wanted"})
    )

    assert (index.parsed_sources, index.skipped_sources) == (1, 1)
    assert index.names_a_value("wanted") is True


def test_a_narrowed_index_gives_the_same_answer_as_the_whole_read():
    wide = value_references.build_reference_index(_sources())
    narrow = value_references.build_reference_index(
        _sources(), names=frozenset({"wanted"})
    )

    assert narrow.names_a_value("wanted") == wide.names_a_value("wanted")


def test_a_narrowed_index_refuses_a_name_it_never_read_for():
    """Fail closed: answering "nothing names it" from unread sources is a lie."""
    index = value_references.build_reference_index(
        _sources(), names=frozenset({"wanted"})
    )

    with pytest.raises(ValueError):
        index.names_a_value("unrelated")


def test_the_report_states_what_the_scan_skipped():
    index = value_references.build_reference_index(
        _sources(), names=frozenset({"wanted"})
    )

    assert index.as_report()["reference_skipped_sources"] == 1
