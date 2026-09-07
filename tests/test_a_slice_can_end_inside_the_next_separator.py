"""A day that grew keeps the citations written before it grew.

An appender writes its own separator, so the newlines between the last entry
of the old file and the first byte of the new one arrived with the new entry.
A slice that ended at the old file's end therefore ends inside that run, not
at the entry offset — and only the entry offset was ever tried.

Measured on this vault: `knowledge/daily/2026-09-02.md` was 2294 bytes when it
was compiled, ending `session close.\\n`; the next append wrote one more `\\n`
before its own block, so the following entry starts at 2295. Four claims on
two pages read `evidence_unresolved` for that one byte.
"""

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from evidence_resolver import compile_part_slice  # noqa: E402

MARKER = b"<!-- llm-wiki-operation:"
ENTRY = MARKER + b"a1 -->\n\nSomething happened, and it was written down.\n"
LATER = MARKER + b"b2 -->\n\nSomething else happened, later.\n"


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def test_a_compiled_prefix_is_found_after_one_separator_newline():
    compiled = ENTRY
    grown = compiled + b"\n" + LATER

    assert compile_part_slice(grown, _digest(compiled)) == compiled


def test_it_is_found_after_a_run_of_separators():
    compiled = ENTRY
    grown = compiled + b"\n\n\n" + LATER

    assert compile_part_slice(grown, _digest(compiled)) == compiled


def test_a_day_that_only_grew_by_whole_entries_still_resolves():
    compiled = ENTRY
    grown = compiled + LATER

    assert compile_part_slice(grown, _digest(compiled)) == compiled


def test_bytes_that_were_changed_are_still_refused():
    compiled = ENTRY
    edited = ENTRY.replace(b"written down", b"quietly rewritten") + b"\n" + LATER

    assert compile_part_slice(edited, _digest(compiled)) is None


def test_the_whole_file_is_still_a_candidate():
    whole = ENTRY + b"\n" + LATER

    assert compile_part_slice(whole, _digest(whole)) == whole
