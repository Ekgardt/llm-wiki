"""Sealing a full journal does not drop blockers that nobody closed.

The session handoff shows every open blocker, but the rotation snapshot kept only
the last five: seven open blockers before a rotation were five after it.
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

import project_journal  # noqa: E402
from project_journal import ProjectStore  # noqa: E402
from test_project_journal import checkpoint_event  # noqa: E402


def _blockers(count: int) -> list[dict[str, str]]:
    return [
        {"id": f"blocker-{index}", "action": "upsert", "value": f"waiting on {index}"}
        for index in range(1, count + 1)
    ]


def test_seven_open_blockers_are_seven_after_the_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    (vault / "knowledge/projects/demo").mkdir(parents=True)
    store = ProjectStore(vault, tmp_path / "state")
    store.checkpoint(
        "demo", checkpoint_event("evt-1", "blk:1", delta={"blockers": _blockers(7)}), "agent-a"
    )
    before = list(store.projection("demo").blockers)

    monkeypatch.setattr(project_journal, "ROTATE_ABOVE_BYTES", 1)
    store.checkpoint("demo", checkpoint_event("evt-2", "blk:2", delta={"blockers": []}), "agent-a")

    sealed = list((vault / "knowledge/projects/demo").glob("journal.0*.md"))
    assert len(sealed) == 1
    assert list(store.projection("demo").blockers) == before
    assert len(before) == 7
