"""The validation a publication runs is inside the caller's bound (G-M7, third point).

Research: `docs/research/2026-09-17-a-publication-that-is-out-of-time-does-not-link.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for directory in (TESTS.parent / "scripts", TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_evidence_graph import basic_graph_records  # noqa: E402


class _StopOnceTheDatabaseIsBuilt:
    """Cancels on the ask after the build's own last one, which is the publication's.

    The build writes its rows inside one transaction, so its rollback journal is
    on disk until the commit; after the commit the journal is gone and the
    temporary database is there. The build asks once more in that state, and the
    next ask in that state comes from the publication.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.settled = 0

    def _built(self) -> bool:
        temporary = [path for path in self.directory.glob(".*.tmp")]
        if not temporary:
            return False
        return not (self.directory / f"{temporary[0].name}-journal").exists()

    def __call__(self) -> bool:
        if not self._built():
            return False
        self.settled += 1
        return self.settled > 1


def test_a_cancelled_build_does_not_validate_and_publish_anyway(tmp_path: Path) -> None:
    from evidence_graph import create_generation_database

    path = tmp_path / "evidence.sqlite3"

    with pytest.raises(TimeoutError):
        create_generation_database(
            path, cancelled=_StopOnceTheDatabaseIsBuilt(tmp_path), **basic_graph_records()
        )

    assert (path.exists(), list(tmp_path.glob(".*.tmp"))) == (False, [])
