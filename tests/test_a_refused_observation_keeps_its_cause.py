"""An unreadable observation retains its redacted cause and grants no permit."""
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

import pytest

from tests.test_runtime_deletion_contract import _vault, build_adopted_reliability_v3


def _observed(tmp_path, monkeypatch, scan):
    import doctor
    import installed_memory_repair

    root, state = _vault(tmp_path)
    build_adopted_reliability_v3(root, state)
    monkeypatch.setattr(installed_memory_repair, "validate_reliability_v3_runtime", scan(state))
    result = doctor._run_deletion_check(state, datetime.now(timezone.utc), root=root)
    assert result["quiescent"] is False
    assert result["permit"] is False
    assert {item["code"] for item in result["blockers"]} == {"run_deletion_state_unknown"}
    return result


def _locked_sqlite_scan(state):
    path = state / "run/markdown-transactions-v3.sqlite3"

    def scan(**_kwargs):
        with closing(sqlite3.connect(path, timeout=0)) as owner:
            owner.execute("BEGIN EXCLUSIVE")
            try:
                with closing(sqlite3.connect(path, timeout=0)) as reader:
                    return reader.execute("SELECT * FROM maintenance_owners").fetchall()
            finally:
                owner.rollback()

    return scan


def test_busy_database_observation_names_the_real_sqlite_error(tmp_path, monkeypatch):
    result = _observed(tmp_path, monkeypatch, _locked_sqlite_scan)
    assert result["observation_error"] == "OperationalError: database is locked"


def _permission_scan(state):
    directory = state / "run/refused-replacement"
    directory.mkdir()
    before = directory / "before"
    after = directory / "after"
    before.write_bytes(b"original")
    after.write_bytes(b"replacement")

    def scan(**_kwargs):
        directory.chmod(0o500)
        try:
            return os.replace(after, before)
        finally:
            directory.chmod(0o700)

    return scan


@pytest.mark.skipif(os.name != "posix", reason="real POSIX directory permission refusal")
def test_refused_file_replacement_names_the_real_permission_error(tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root bypasses POSIX directory permissions")
    result = _observed(tmp_path, monkeypatch, _permission_scan)
    assert result["observation_error"].startswith("PermissionError:")
