"""After `--rebuild`, the checkpoint that asked for the rebuild settles and the project moves on.

A checkpoint that met a journal in need of a rebuild was parked as `quarantined`.
Recovery replays only `prepared` and `reserved` rows, and every later sequence
waits behind a row that is not committed — so after the rebuild the project was
still blocked, and only the byte-identical original request could clear it.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS.parent / "scripts"
for entry in (str(SCRIPTS_DIR), str(TESTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import project_journal  # noqa: E402
from project_journal import ProjectJournalRebuildRequired, ProjectStore  # noqa: E402
from test_project_journal import checkpoint_event  # noqa: E402


def _event(index: int) -> dict[str, object]:
    return checkpoint_event(f"evt-{index}", f"rb:event-{index}")


def test_the_parked_checkpoint_commits_and_the_next_one_is_accepted(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    (vault / "knowledge/projects/demo").mkdir(parents=True)
    store = ProjectStore(vault, tmp_path / "state")
    for index in (1, 2, 3):
        store.checkpoint("demo", _event(index), "agent-a")
    shutil.rmtree(vault / "knowledge/projects/demo")
    with pytest.raises(ProjectJournalRebuildRequired):
        store.checkpoint("demo", _event(4), "agent-a")

    store.rebuild_journal("demo")
    recovered = store.recover("demo")
    receipt = store.checkpoint("demo", _event(5), "agent-a")

    journal = (vault / "knowledge/projects/demo/journal.md").read_bytes()
    sequences = [e["sequence"] for e in project_journal.parse_journal_events("demo", journal)]
    assert [item.sequence for item in recovered] == [4]
    assert (receipt.sequence, sequences) == (5, [1, 2, 3, 4, 5])
