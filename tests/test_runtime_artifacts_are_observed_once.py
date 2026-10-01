"""One current artifact observation feeds database identity and retention checks."""

import sqlite3
import time
from pathlib import Path

import installed_memory_repair as repair
import pytest

from tests.slow_machine import LONG_TIMEOUT
from tests.test_the_installed_check_reads_every_row import (
    _INSERT,
    _adopted_with_rows,
    _coordinator_blockers,
    _old_committed,
)


def test_runtime_artifacts_are_enumerated_once(tmp_path, monkeypatch):
    state = _adopted_with_rows(tmp_path, _old_committed(1))
    directory = state / "run" / "transactions"
    (directory / "t000000").mkdir(parents=True)
    (directory / "unrecorded").mkdir()
    (directory / ".t000001.pruning-test").mkdir()
    original = repair._artifact_entries
    observations = []

    def observe(root, deadline):
        entries = original(root, deadline)
        observations.append(entries)
        return entries

    monkeypatch.setattr(repair, "_artifact_entries", observe)
    blockers = _coordinator_blockers(state)
    assert "transaction_artifact_state_unknown" in blockers
    assert "transaction_prune_interrupted" in blockers
    assert "transaction_state_unreadable" not in blockers
    assert len(observations) == 1


def test_runtime_scan_resolves_root_per_observation(tmp_path, monkeypatch):
    directory = tmp_path / "artifacts"
    directory.mkdir()
    for name in ("one", "two", "three"):
        (directory / name).touch()
    original = Path.resolve
    resolutions = []

    def resolve(path, *args, **kwargs):
        resolutions.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    entries = repair._bounded_entries(directory, state_root=tmp_path, deadline=time.monotonic() + LONG_TIMEOUT)
    assert len(entries) == 3
    assert resolutions.count(tmp_path) == 2


def test_runtime_artifact_escape_is_refused(tmp_path):
    state = _adopted_with_rows(tmp_path, _old_committed(1))
    directory = state / "run" / "transactions"
    directory.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (directory / "t000000").symlink_to(outside, target_is_directory=True)
    assert "transaction_state_unreadable" in _coordinator_blockers(state)


def test_runtime_root_retargeted_during_scan_is_refused(tmp_path, monkeypatch):
    original_root = tmp_path / "original"
    original_root.mkdir()
    (original_root / "entry").touch()
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    alias = tmp_path / "state"
    alias.symlink_to(original_root, target_is_directory=True)
    original = repair._contained_runtime_entry

    def retarget(value, root):
        alias.unlink()
        alias.symlink_to(replacement, target_is_directory=True)
        return original(value, root)

    monkeypatch.setattr(repair, "_contained_runtime_entry", retarget)
    with pytest.raises(PermissionError):
        repair._bounded_entries(original_root, state_root=alias, deadline=time.monotonic() + LONG_TIMEOUT)


def test_concurrent_unrecorded_artifact_remains_visible(tmp_path, monkeypatch):
    state = _adopted_with_rows(tmp_path, _old_committed(1))
    directory = state / "run" / "transactions"
    directory.mkdir(exist_ok=True)
    original = repair._artifact_entries

    def observe(root, deadline):
        (directory / "concurrent").mkdir(exist_ok=True)
        return original(root, deadline)

    monkeypatch.setattr(repair, "_artifact_entries", observe)
    assert "transaction_artifact_state_unknown" in _coordinator_blockers(state)


def test_artifact_removed_during_identity_check_is_unreadable(tmp_path, monkeypatch):
    state = _adopted_with_rows(tmp_path, _old_committed(1))
    directory = state / "run" / "transactions"
    target = directory / "t000000"
    target.mkdir(parents=True)
    original = repair._transaction_artifact_id

    def removed(entry: Path):
        entry.rmdir()
        return original(entry)

    monkeypatch.setattr(repair, "_transaction_artifact_id", removed)
    assert "transaction_state_unreadable" in _coordinator_blockers(state)


def test_transaction_committed_after_enumeration_is_recognized(tmp_path, monkeypatch):
    state = _adopted_with_rows(tmp_path, [])
    target = state / "run" / "transactions" / "t000000"
    target.mkdir(parents=True)
    original = repair._artifact_entries

    def observe(root, deadline):
        entries = original(root, deadline)
        with sqlite3.connect(state / "run" / "markdown-transactions-v3.sqlite3") as db:
            db.executemany(_INSERT, _old_committed(1))
        return entries

    monkeypatch.setattr(repair, "_artifact_entries", observe)
    blockers = _coordinator_blockers(state)
    assert "transaction_artifact_state_unknown" not in blockers
    assert "transaction_state_unreadable" not in blockers
