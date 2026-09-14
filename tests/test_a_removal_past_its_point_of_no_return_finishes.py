"""A generation removal that has deleted its tree commits, and one that did not is completed.

A deadline checked after `rmtree` rolled the rows back over a tree that was gone, and
every later prune reported it unpaired and failed. Research:
`docs/research/2026-09-14-a-removal-past-its-point-of-no-return-finishes.md`.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
TESTS = Path(__file__).resolve().parent
for directory in (SCRIPTS, TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_prune_generations import _catalog, _chain, _footprint  # noqa: E402


def test_a_cancel_that_arrives_after_the_tree_is_gone_does_not_undo_the_rows(tmp_path, monkeypatch):
    import generation_catalog

    catalog = _catalog(tmp_path)
    _chain(catalog, ["gen-1", "gen-2", "gen-3"])
    removed: list[str] = []
    real_rmtree = shutil.rmtree
    monkeypatch.setattr(generation_catalog.shutil, "rmtree", lambda path: removed.append(str(path)) or real_rmtree(path))

    catalog.discard_superseded("gen-1", cancelled=lambda: bool(removed))

    assert _footprint(catalog, "gen-1") == (False, 0, 0)


def test_a_discard_whose_commit_was_lost_is_completed_by_the_next_prune(tmp_path):
    import prune_generations

    catalog = _catalog(tmp_path)
    _chain(catalog, ["gen-1", "gen-2", "gen-3"])
    shutil.rmtree(catalog.generations_path / "gen-1")

    lines = prune_generations.prune_generations(state_root=catalog.state_root, apply=True)

    assert (_footprint(catalog, "gen-1"), prune_generations._report(lines)) == ((False, 0, 0), 0)
