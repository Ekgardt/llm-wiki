"""A journal rebuild holds the project, as both sibling repairs do.

Without the lease, a checkpoint committed between the read of the committed
events and the write of `journal.md` was left out of the rebuilt journal: the row
survived, but the file the operator was handed was already behind.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from project_journal import ProjectLeaseBusy, ProjectStore  # noqa: E402

from tests.test_project_journal import checkpoint_event  # noqa: E402

_SLUG = "demo"


def _store(tmp_path: Path) -> ProjectStore:
    root = tmp_path / "vault"
    (root / f"knowledge/projects/{_SLUG}").mkdir(parents=True)
    (root / "knowledge/notes").mkdir(parents=True)
    return ProjectStore(root, tmp_path / "state")


def _checkpoint(store: ProjectStore, name: str) -> None:
    store.checkpoint(_SLUG, checkpoint_event(name, f"task:{name}:active"), "agent-a")


def test_the_rebuild_is_refused_while_another_holds_the_project(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _checkpoint(store, "first")
    holder = store.acquire_lease(_SLUG, "writer")

    try:
        with pytest.raises(ProjectLeaseBusy):
            store.rebuild_journal(_SLUG)
    finally:
        store._release(holder)


def test_the_rebuild_releases_the_project_when_it_is_done(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _checkpoint(store, "first")

    report = store.rebuild_journal(_SLUG)
    after = store.acquire_lease(_SLUG, "writer")

    try:
        assert (report["project"], report["events"]) == (_SLUG, 1)
    finally:
        store._release(after)


def test_the_rebuild_releases_the_project_when_it_fails(tmp_path: Path) -> None:
    """A project with nothing committed still gives the lease back."""
    store = _store(tmp_path)

    with pytest.raises(Exception):
        store.rebuild_journal(_SLUG)

    after = store.acquire_lease(_SLUG, "writer")
    store._release(after)
