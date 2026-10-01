"""SQLite-created files must be private from birth, not repaired after writes."""

from __future__ import annotations

import contextlib
import os
import sqlite3

import memory_queue
import pytest
import reliable_memory

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native Windows DACL inheritance")


def test_new_runtime_children_inherit_only_the_owner(tmp_path):
    runtime = tmp_path / "run"
    reliable_memory.validate_state_root(runtime)
    nested = runtime / "nested"
    nested.mkdir()
    child = nested / "private.txt"
    child.write_bytes(b"private runtime data")
    assert memory_queue._is_owner_only(runtime)
    assert memory_queue._is_owner_only(nested)
    assert memory_queue._is_owner_only(child)


def test_sqlite_created_wal_and_shm_are_private_before_validation(tmp_path):
    runtime = tmp_path / "run"
    reliable_memory.validate_state_root(runtime)
    database = runtime / "queue-v3.sqlite3"
    with contextlib.closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("CREATE TABLE evidence(value TEXT)")
        connection.execute("INSERT INTO evidence VALUES ('private payload')")
        connection.commit()
        for suffix in ("", "-wal", "-shm"):
            path = database.with_name(database.name + suffix)
            assert path.is_file()
            assert memory_queue._is_owner_only(path), path.name
        reliable_memory._validate_operational_sidecars(database, runtime)


def test_explicit_foreign_sidecar_access_is_still_refused(tmp_path):
    import markdown_transaction

    runtime = tmp_path / "run"
    reliable_memory.validate_state_root(runtime)
    sidecar = runtime / "queue-v3.sqlite3-wal"
    sidecar.write_bytes(b"private payload")
    changed = markdown_transaction._run_acl_command([
        "icacls", str(sidecar), "/grant", "*S-1-1-0:(R)",
    ])
    assert changed.returncode == 0
    with pytest.raises(PermissionError, match="owner-only"):
        reliable_memory._validate_operational_sidecar(sidecar, runtime)
