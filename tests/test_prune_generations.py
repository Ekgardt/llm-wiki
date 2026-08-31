"""Retention for published evidence-graph generations.

Nothing collected them before this: `discard_unactivated` refuses anything that
was ever activated, so a vault that builds nightly accumulates every generation
it has ever published. These tests pin what retention keeps (the active
generation and the ancestors the fallback chain still reaches), what it drops
(registration, activation history and directory, together), and what it refuses
to judge (a registration and a tree that disagree).
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from test_generation_catalog import _catalog, _publish  # noqa: E402


def _chain(catalog, names: list[str]) -> None:
    """Publish and activate a linear parent chain, oldest first."""
    previous = None
    for name in names:
        _publish(catalog, name, parent=previous)
        catalog.register(name)
        assert catalog.activate(name, expected_active=previous) is True
        previous = name


def _rows(catalog, table: str, identifier: str) -> int:
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        return database.execute(
            f"SELECT COUNT(*) FROM {table} WHERE generation_id = ?", (identifier,)
        ).fetchone()[0]


def _footprint(catalog, identifier: str) -> tuple[bool, int, int]:
    """Directory, registration and activation history, as one observation."""
    return (
        (catalog.generations_path / identifier).is_dir(),
        _rows(catalog, "generations", identifier),
        _rows(catalog, "activation_history", identifier),
    )


def _present(catalog, names: list[str]) -> list[bool]:
    return [(catalog.generations_path / name).is_dir() for name in names]


def _prune_step_commands() -> list[list[str]]:
    import scheduled_weekly

    steps = scheduled_weekly._script_steps()
    return [command for _message, _label, command, _timeout in steps]


def test_the_active_generation_and_its_parent_are_the_live_set(tmp_path):
    catalog = _catalog(tmp_path)
    _chain(catalog, ["gen-1", "gen-2", "gen-3", "gen-4"])

    assert catalog.retained_generations() == ("gen-4", "gen-3")


def test_retention_depth_is_a_parameter_of_the_walk(tmp_path):
    catalog = _catalog(tmp_path)
    _chain(catalog, ["gen-1", "gen-2", "gen-3", "gen-4"])

    assert catalog.retained_generations(retained_ancestors=0) == ("gen-4",)
    assert catalog.retained_generations(retained_ancestors=2) == (
        "gen-4",
        "gen-3",
        "gen-2",
    )


def test_the_active_generation_is_never_discarded(tmp_path):
    catalog = _catalog(tmp_path)
    _chain(catalog, ["gen-1", "gen-2"])

    with pytest.raises(ValueError, match="retained generation"):
        catalog.discard_superseded("gen-2")

    assert _footprint(catalog, "gen-2") == (True, 1, 1)


def test_the_parent_of_the_active_generation_is_never_discarded(tmp_path):
    """Reuse and the fallback chain both stand on it: 148 s versus 4 s here."""
    catalog = _catalog(tmp_path)
    _chain(catalog, ["gen-1", "gen-2", "gen-3"])

    with pytest.raises(ValueError, match="retained generation"):
        catalog.discard_superseded("gen-2")

    assert _footprint(catalog, "gen-2") == (True, 1, 1)


def test_a_superseded_generation_loses_row_history_and_tree_together(tmp_path):
    catalog = _catalog(tmp_path)
    _chain(catalog, ["gen-1", "gen-2", "gen-3"])
    assert _footprint(catalog, "gen-1") == (True, 1, 1)

    assert catalog.discard_superseded("gen-1") is True

    assert _footprint(catalog, "gen-1") == (False, 0, 0)


def test_a_failed_tree_removal_rolls_the_registration_back(tmp_path, monkeypatch):
    """No reader may see a row without its tree: nothing commits until the
    directory is actually gone."""
    import generation_catalog

    catalog = _catalog(tmp_path)
    _chain(catalog, ["gen-1", "gen-2", "gen-3"])

    def refuse(path):
        raise OSError("filesystem refused the removal")

    monkeypatch.setattr(generation_catalog.shutil, "rmtree", refuse)

    with pytest.raises(OSError, match="filesystem refused"):
        catalog.discard_superseded("gen-1")

    assert _footprint(catalog, "gen-1") == (True, 1, 1)


def test_a_registration_without_a_tree_is_refused_not_deleted(tmp_path):
    catalog = _catalog(tmp_path)
    _chain(catalog, ["gen-1", "gen-2", "gen-3"])
    shutil.rmtree(catalog.generations_path / "gen-1")

    with pytest.raises(ValueError, match="tree is missing"):
        catalog.discard_superseded("gen-1")

    assert _rows(catalog, "generations", "gen-1") == 1


def test_an_unregistered_tree_is_refused_not_deleted(tmp_path):
    catalog = _catalog(tmp_path)
    _chain(catalog, ["gen-1", "gen-2"])
    orphan = catalog.generations_path / "gen-orphan"
    orphan.mkdir()
    (orphan / "search.sqlite3").write_bytes(b"stray")

    with pytest.raises(ValueError, match="not registered"):
        catalog.discard_superseded("gen-orphan")

    assert (orphan / "search.sqlite3").exists()


def test_a_publication_in_flight_is_refused_not_deleted(tmp_path):
    """`register` returns before `activate` is called. A never-activated
    registration is an abort or a live build, and nothing here tells them
    apart, so retention leaves it to the fenced `discard_unactivated`."""
    catalog = _catalog(tmp_path)
    _chain(catalog, ["gen-1", "gen-2", "gen-3"])
    _publish(catalog, "gen-flight", parent="gen-3")
    catalog.register("gen-flight")

    with pytest.raises(ValueError, match="never activated"):
        catalog.discard_superseded("gen-flight")

    assert _footprint(catalog, "gen-flight") == (True, 1, 0)


def test_the_plan_names_the_kept_the_dropped_the_unpaired_and_the_pending(tmp_path):
    import prune_generations

    catalog = _catalog(tmp_path)
    _chain(catalog, ["gen-1", "gen-2", "gen-3", "gen-4"])
    _publish(catalog, "gen-flight", parent="gen-4")
    catalog.register("gen-flight")
    (catalog.generations_path / "gen-orphan").mkdir()

    plan = prune_generations.plan_prune(catalog)

    assert (plan.retained, plan.prunable, plan.unpaired, plan.pending) == (
        ("gen-4", "gen-3"),
        ("gen-1", "gen-2"),
        ("gen-orphan",),
        ("gen-flight",),
    )


def test_a_pending_publication_survives_the_applied_pass(tmp_path):
    import prune_generations

    catalog = _catalog(tmp_path)
    _chain(catalog, ["gen-1", "gen-2", "gen-3"])
    _publish(catalog, "gen-flight", parent="gen-3")
    catalog.register("gen-flight")

    lines = prune_generations.prune_generations(
        state_root=catalog.state_root, apply=True
    )

    assert "PENDING: gen-flight: registered but never activated" in lines
    assert _footprint(catalog, "gen-flight") == (True, 1, 0)
    assert prune_generations._report(lines) == 0


def test_a_dry_run_removes_nothing(tmp_path):
    import prune_generations

    catalog = _catalog(tmp_path)
    _chain(catalog, ["gen-1", "gen-2", "gen-3"])

    lines = prune_generations.prune_generations(state_root=catalog.state_root)

    assert "would remove gen-1" in lines
    assert _present(catalog, ["gen-1", "gen-2", "gen-3"]) == [True, True, True]


def test_applying_the_policy_reclaims_the_superseded_trees(tmp_path):
    import prune_generations

    catalog = _catalog(tmp_path)
    _chain(catalog, ["gen-1", "gen-2", "gen-3"])
    doomed = prune_generations._directory_bytes(catalog.generations_path / "gen-1")

    lines = prune_generations.prune_generations(
        state_root=catalog.state_root, apply=True
    )

    assert _present(catalog, ["gen-1", "gen-2", "gen-3"]) == [False, True, True]
    assert [line for line in lines if line.startswith("removed ")] == [
        f"removed gen-1 ({doomed} bytes)"
    ]
    assert lines[-1] == f"reclaimed {doomed} bytes"


def test_an_unpaired_generation_makes_the_pass_report_a_failure(tmp_path):
    import prune_generations

    catalog = _catalog(tmp_path)
    _chain(catalog, ["gen-1", "gen-2", "gen-3"])
    (catalog.generations_path / "gen-orphan").mkdir()

    lines = prune_generations.prune_generations(
        state_root=catalog.state_root, apply=True
    )

    assert "UNPAIRED: gen-orphan: registration and tree disagree" in lines
    assert prune_generations._report(lines) == 1


def test_a_catalog_with_no_active_pointer_collects_nothing(tmp_path):
    """`_repair_active_pointer` clears the pointer when nothing validates.
    Collecting then would eat the material a repair reads."""
    import prune_generations

    catalog = _catalog(tmp_path)
    _publish(catalog, "gen-1")
    catalog.register("gen-1")

    lines = prune_generations.prune_generations(
        state_root=catalog.state_root, apply=True
    )

    assert lines[0].startswith("ERROR: catalog names no active generation")
    assert _footprint(catalog, "gen-1") == (True, 1, 0)


def test_the_weekly_pass_prunes_generations():
    """Retention runs unattended or it does not run: the owner is not asked."""
    prune = [
        command
        for command in _prune_step_commands()
        if Path(command[1]).name == "prune_generations.py"
    ]

    assert len(prune) == 1
    assert "--apply" in prune[0]
