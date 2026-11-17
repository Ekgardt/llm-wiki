"""The per-edit diff runs where a deadline and a cancel reach it (audit 2026-09-27 A-5).

docs/research/2026-09-27-an-impact-diff-can-be-stopped.md
"""
from __future__ import annotations

import time

import impact_analysis

from tests.slow_machine import SHORT_TIMEOUT


def _every_seventh_changed(count: int) -> tuple[bytes, bytes]:
    old = b"x = 1\n" * count
    new = b"".join(b"y = 2\n" if index % 7 == 0 else b"x = 1\n" for index in range(count))
    return old, new


def test_a_file_of_repeated_lines_is_diffed_before_its_deadline_and_exactly() -> None:
    old, new = _every_seventh_changed(10_000)
    deadline = time.monotonic() + SHORT_TIMEOUT

    ranges = impact_analysis.changed_ranges(old, new, deadline=deadline)

    assert (time.monotonic() < deadline, len(ranges)) == (True, len(range(0, 10_000, 7)))


def test_each_hunk_names_its_own_line() -> None:
    old = b"".join(f"value_{index} = {index}\n".encode() for index in range(50))
    new = b"".join(
        f"value_{index} = {index + 1 if index % 7 == 0 else index}\n".encode() for index in range(50)
    )

    hunks = impact_analysis.diff_hunks(
        old.splitlines(keepends=True), new.splitlines(keepends=True), 0, 0, (None, None)
    )

    assert hunks == [(line, line + 1, line, line + 1) for line in range(0, 50, 7)]
