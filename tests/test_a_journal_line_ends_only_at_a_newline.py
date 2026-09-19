"""A Unicode line separator inside a checkpoint's text does not wedge the journal.

The journal is JSON Lines. Canonical JSON writes U+2028, U+2029 and U+0085 raw
inside a string, and the reader split with `str.splitlines()`, which breaks at
all three: one such character, and every later read and checkpoint raised.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS.parent / "scripts"
for entry in (str(SCRIPTS_DIR), str(TESTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from project_journal import ProjectStore  # noqa: E402
from test_project_journal import checkpoint_event  # noqa: E402


def _event_with(separator: str) -> dict[str, object]:
    event = checkpoint_event("evt-1", "sep:event-1")
    event["reason"] = f"line one{separator}line two"
    return event


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
def test_the_project_still_reads_and_takes_the_next_checkpoint(
    tmp_path: Path, separator: str
) -> None:
    vault = tmp_path / "vault"
    (vault / "knowledge/projects/demo").mkdir(parents=True)
    store = ProjectStore(vault, tmp_path / "state")
    store.checkpoint("demo", _event_with(separator), "agent-a")

    projection = store.projection("demo")
    receipt = store.checkpoint("demo", checkpoint_event("evt-2", "sep:event-2"), "agent-a")

    assert (projection.last_applied_sequence, receipt.sequence) == (1, 2)
