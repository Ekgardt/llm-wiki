"""Journal mode is a persisted contract, never an ordinary-open side effect."""
from __future__ import annotations

import contextlib
import os
import sqlite3

import pytest
import reliable_memory as memory


@pytest.fixture(params=[0x4C575433, 0x4C575133])
def wal_database(tmp_path, request):
    _require_fixed_test_runtime()
    path = tmp_path / "run" / "coordination.sqlite3"
    contract = memory.OperationalDatabaseContract(application_id=request.param)
    with contextlib.closing(memory.open_operational_db(
        path, busy_ms=0, contract=contract, initialize_contract=True
    )) as database:
        database.execute("CREATE TABLE sample(value INTEGER NOT NULL)")
        database.execute("INSERT INTO sample VALUES (0)")
        assert database.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    return path, contract


def test_ordinary_writer_preserves_wal(wal_database):
    path, contract = wal_database
    with contextlib.closing(memory.open_operational_db(
        path, busy_ms=0, contract=contract
    )) as writer:
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert writer.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_held_reader_does_not_prevent_writer_commit(wal_database):
    path, contract = wal_database
    with contextlib.closing(memory.open_readonly_operational_db(
        path, path.parent, max_bytes=path.stat().st_size, contract=contract
    )) as reader, contextlib.closing(memory.open_operational_db(
        path, busy_ms=0, contract=contract
    )) as writer:
        reader.execute("BEGIN")
        assert reader.execute("SELECT value FROM sample").fetchone()[0] == 0
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE sample SET value=1")
        writer.execute("COMMIT")
        assert reader.execute("SELECT value FROM sample").fetchone()[0] == 0
        reader.execute("COMMIT")
        assert reader.execute("SELECT value FROM sample").fetchone()[0] == 1
        assert writer.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_unrecognized_database_cannot_silently_change_mode(tmp_path):
    path = tmp_path / "run" / "other.sqlite3"
    with contextlib.closing(memory.open_operational_db(path, busy_ms=0)) as db:
        db.execute("CREATE TABLE sample(value)")
        db.execute("PRAGMA journal_mode=WAL")
    with pytest.raises(memory.OperationalDatabaseContractError, match="WAL"):
        memory.open_operational_db(path, busy_ms=0)
    with contextlib.closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


@pytest.mark.parametrize("version", [(3, 44, 5), (3, 49, 9), (3, 50, 6), (3, 51, 2)])
def test_unsafe_runtime_cannot_open_wal(wal_database, monkeypatch, version):
    path, contract = wal_database
    monkeypatch.setattr(sqlite3, "sqlite_version_info", version)
    with pytest.raises(memory.OperationalDatabaseContractError, match="WAL-reset"):
        memory.open_operational_db(path, busy_ms=0, contract=contract)


@pytest.mark.parametrize("version", [(3, 44, 6), (3, 50, 7), (3, 51, 3), (3, 53, 1)])
def test_documented_wal_reset_fixes_are_accepted(monkeypatch, version):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", version)
    memory.require_safe_wal_runtime()


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
@pytest.mark.parametrize("opener", ["reader", "writer"])
def test_sidecar_symlink_refused_before_sqlite_open(wal_database, suffix, opener):
    path, contract = wal_database
    target = path.parent / "must-not-be-touched"
    target.write_bytes(b"private evidence")
    path.with_name(path.name + suffix).symlink_to(target)
    with pytest.raises(PermissionError, match="regular file"):
        _open(path, contract, opener)
    assert target.read_bytes() == b"private evidence"


def _open(path, contract, opener):
    if opener == "writer":
        return memory.open_operational_db(path, busy_ms=0, contract=contract)
    return memory.open_readonly_operational_db(
        path, path.parent, max_bytes=path.stat().st_size, contract=contract
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits; Windows uses ACLs")
@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_sidecar_permissions_are_not_silently_repaired(wal_database, suffix):
    path, contract = wal_database
    sidecar = path.with_name(path.name + suffix)
    sidecar.write_bytes(b"invalid")
    sidecar.chmod(0o644)
    with pytest.raises(PermissionError, match="owner-only"):
        memory.open_operational_db(path, busy_ms=0, contract=contract)
    assert sidecar.read_bytes() == b"invalid"


def test_wrong_contract_is_still_rejected(wal_database):
    path, _contract = wal_database
    with pytest.raises(memory.OperationalDatabaseContractError, match="application_id"):
        memory.open_operational_db(
            path, busy_ms=0,
            contract=memory.OperationalDatabaseContract(application_id=123),
        )


def test_online_backup_includes_uncheckpointed_commits(wal_database, tmp_path):
    import time

    from private_vault_backup import _sqlite_online_backup

    path, contract = wal_database
    destination = tmp_path / "backup.sqlite3"
    with contextlib.closing(_open(path, contract, "reader")) as reader:
        reader.execute("BEGIN")
        assert reader.execute("SELECT value FROM sample").fetchone()[0] == 0
        with contextlib.closing(_open(path, contract, "writer")) as writer:
            writer.execute("UPDATE sample SET value=7")
            _sqlite_online_backup(path, destination, time.monotonic() + 30)
            with contextlib.closing(sqlite3.connect(destination)) as restored:
                assert restored.execute("SELECT value FROM sample").fetchone()[0] == 7
                assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert reader.execute("SELECT value FROM sample").fetchone()[0] == 0


def _require_fixed_test_runtime():
    try:
        memory.require_safe_wal_runtime()
    except memory.OperationalDatabaseContractError:
        pytest.skip("WAL qualification requires the SQLite WAL-reset fix")


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_hardlinked_sidecar_is_refused(wal_database, suffix):
    path, contract = wal_database
    target = path.parent / "retained-file"
    target.write_bytes(b"do not change")
    target.chmod(0o600)
    os.link(target, path.with_name(path.name + suffix))
    with pytest.raises(PermissionError, match="hard links"):
        memory.open_operational_db(path, busy_ms=0, contract=contract)
    assert target.read_bytes() == b"do not change"


@pytest.mark.skipif(os.name == "nt", reason="POSIX unlink of an open file")
@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
@pytest.mark.parametrize("opener", ["reader", "writer"])
def test_unlinked_sidecar_metadata_does_not_mean_multiple_links(wal_database, monkeypatch, suffix, opener):
    from pathlib import Path

    path, contract = wal_database
    sidecar = path.with_name(path.name + suffix)
    sidecar.write_bytes(b"")
    sidecar.chmod(0o600)
    with sidecar.open("rb") as held:
        sidecar.unlink()
        removed = os.fstat(held.fileno())
    assert removed.st_nlink == 0
    original = Path.lstat
    observations = [removed]

    def raced_lstat(target, *args, **kwargs):
        if target == sidecar and observations:
            return observations.pop()
        return original(target, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", raced_lstat)
    with contextlib.closing(_open(path, contract, opener)) as database:
        assert database.execute("SELECT value FROM sample").fetchone()[0] == 0
        assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not observations
