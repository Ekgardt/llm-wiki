from __future__ import annotations

import os
import sqlite3
import stat
import unicodedata
from dataclasses import asdict
from pathlib import PurePosixPath

import pytest
from reliable_memory import (
    DEFAULTS,
    UnsafeStateRoot,
    begin_immediate,
    canonical_json_bytes,
    fsync_directory,
    fsync_file,
    open_operational_db,
    restricted_relative_path,
    sha256_bytes,
    validate_state_root,
)


def test_defaults_are_the_approved_bounded_values():
    assert asdict(DEFAULTS) == {
        "markdown_busy_ms": 10_000,
        "queue_busy_ms": 5_000,
        "transaction_retention_days": 30,
        "artifact_retention_days": 30,
        "archive_hot_days": 90,
        "project_lease_seconds": 30,
        "project_heartbeat_seconds": 10,
        "checkpoint_debounce_seconds": 30,
        "checkpoint_fallback_events": 20,
        "queue_lease_seconds": 120,
        "queue_heartbeat_seconds": 40,
        "queue_max_attempts": 8,
        "retry_base_seconds": 30,
        "retry_cap_seconds": 3_600,
        "worker_max_tasks": 20,
        "worker_max_seconds": 600,
        "worker_idle_seconds": 2,
        "priority_min": -100,
        "priority_max": 100,
        "queue_result_retention_days": 30,
        "dead_task_retention_days": None,
    }


def test_canonical_json_is_compact_sorted_normalized_utf8():
    decomposed = "e\N{COMBINING ACUTE ACCENT}"
    assert canonical_json_bytes({"z": decomposed, "a": [True, None, 2]}) == (
        '{"a":[true,null,2],"z":"é"}'.encode()
    )


@pytest.mark.parametrize("value", [1.5, {"value": 1.5}, [1.5]])
def test_canonical_json_rejects_floats(value):
    with pytest.raises((TypeError, ValueError), match="float"):
        canonical_json_bytes(value)


def test_canonical_json_rejects_non_string_keys_and_normalized_collisions():
    with pytest.raises(TypeError, match="string"):
        canonical_json_bytes({1: "value"})

    composed = unicodedata.normalize("NFC", "e\N{COMBINING ACUTE ACCENT}")
    decomposed = unicodedata.normalize("NFD", composed)
    with pytest.raises(ValueError, match="collision"):
        canonical_json_bytes({composed: 1, decomposed: 2})


def test_sha256_is_stable_for_equal_logical_objects():
    left = canonical_json_bytes({"b": 2, "a": 1})
    right = canonical_json_bytes({"a": 1, "b": 2})
    assert sha256_bytes(left) == sha256_bytes(right)
    assert sha256_bytes(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


@pytest.mark.parametrize(
    "value",
    ["", ".", "../x", "a/../../x", "/absolute", "C:/absolute", "a\\b", "a//b"],
)
def test_restricted_relative_path_rejects_unsafe_values(value):
    with pytest.raises(ValueError):
        restricted_relative_path(value, ("knowledge", "run/transactions"))


def test_restricted_relative_path_accepts_only_allowed_roots():
    assert restricted_relative_path("knowledge/daily/2026-01-01.md", ("knowledge",)) == (
        PurePosixPath("knowledge/daily/2026-01-01.md")
    )
    with pytest.raises(ValueError, match="allowed root"):
        restricted_relative_path("cache/private.json", ("knowledge",))


def test_operational_connection_uses_required_pragmas_and_row_factory(tmp_path):
    path = tmp_path / "run" / "x.sqlite3"
    with open_operational_db(path, busy_ms=10_000) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 10_000
        db.execute("CREATE TABLE sample (name TEXT)")
        db.execute("INSERT INTO sample VALUES ('row')")
        assert db.execute("SELECT name FROM sample").fetchone()["name"] == "row"

    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_begin_immediate_commits_and_rolls_back(tmp_path):
    with open_operational_db(tmp_path / "run" / "x.sqlite3", busy_ms=100) as db:
        db.execute("CREATE TABLE sample (value INTEGER)")
        with begin_immediate(db):
            db.execute("INSERT INTO sample VALUES (1)")
        assert [row["value"] for row in db.execute("SELECT value FROM sample")] == [1]

        with pytest.raises(RuntimeError):
            with begin_immediate(db):
                db.execute("INSERT INTO sample VALUES (2)")
                raise RuntimeError("stop")
        assert [row["value"] for row in db.execute("SELECT value FROM sample")] == [1]


def test_fsync_helpers_accept_files_and_directories(tmp_path):
    path = tmp_path / "data"
    path.write_bytes(b"durable")
    fsync_file(path)
    fsync_directory(tmp_path)


def test_state_root_accepts_normal_local_sqlite_locking(tmp_path):
    validate_state_root(tmp_path / "state")


def test_state_root_rejects_known_network_path(tmp_path, monkeypatch):
    monkeypatch.setattr("reliable_memory._known_network_path", lambda path: True)
    with pytest.raises(UnsafeStateRoot, match="local filesystem"):
        validate_state_root(tmp_path)


def test_state_root_rejects_windows_reparse_point(tmp_path, monkeypatch):
    monkeypatch.setattr("reliable_memory._windows_reparse_point", lambda path: True)
    with pytest.raises(UnsafeStateRoot, match="reparse"):
        validate_state_root(tmp_path)


def test_state_root_rejects_abnormal_sqlite_lock_probe(tmp_path, monkeypatch):
    monkeypatch.setattr("reliable_memory._sqlite_lock_probe", lambda path: False)
    with pytest.raises(UnsafeStateRoot, match="locking"):
        validate_state_root(tmp_path)


def test_state_root_cloud_folder_detection_is_warning_only(tmp_path):
    cloud_root = tmp_path / "OneDrive" / "state"
    with pytest.warns(RuntimeWarning, match="cloud-synchronized"):
        validate_state_root(cloud_root)


def test_normal_lock_contention_is_reported_by_sqlite(tmp_path):
    path = tmp_path / "probe.sqlite3"
    first = sqlite3.connect(path, timeout=0)
    second = sqlite3.connect(path, timeout=0)
    try:
        first.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            second.execute("BEGIN IMMEDIATE")
    finally:
        first.rollback()
        first.close()
        second.close()
