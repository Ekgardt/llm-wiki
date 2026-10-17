"""An adoption identity check must leave a live SQLite writer exclusive."""
import os
import sqlite3
import subprocess
import sys

import pytest
from doctor import _observation_database_identity
from installed_memory_repair import _identity_value, _validate_artifact_reference
from reliable_memory import capture_runtime_file_identity

_LOCK_PROBE = '''
import sqlite3,sys
connection=sqlite3.connect(sys.argv[1],timeout=0)
try:
    connection.execute("BEGIN IMMEDIATE")
except sqlite3.OperationalError:
    print("blocked")
else:
    print("admitted")
finally:
    connection.close()
'''


def _probe(path):
    result = subprocess.run(
        [sys.executable, "-c", _LOCK_PROBE, str(path)],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _adoption_check(record, path, root):
    _validate_artifact_reference(
        record, expected_path=path, state_root=root,
        mutable=True, max_bytes=None,
    )


def _doctor_check(record, path, root):
    del record
    _observation_database_identity(path, root)


@pytest.mark.parametrize("check", [_adoption_check, _doctor_check])
@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor-close lock semantics")
def test_mutable_adoption_identity_preserves_the_writer(tmp_path, check):
    path = tmp_path / "queue.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE evidence (value TEXT)")
    connection.commit()
    record = {"path": path.name, "identity": _identity_value(
        capture_runtime_file_identity(path, state_root=tmp_path)
    )}
    try:
        connection.execute("BEGIN IMMEDIATE")
        assert _probe(path) == "blocked"
        check(record, path, tmp_path)
        assert _probe(path) == "blocked"
    finally:
        connection.close()


def test_database_identity_still_refuses_a_symbolic_link(tmp_path):
    from reliable_memory import capture_operational_database_identity

    source = tmp_path / "database"
    source.write_bytes(b"evidence")
    alias = tmp_path / "alias"
    alias.symlink_to(source)
    with pytest.raises((PermissionError, ValueError)):
        capture_operational_database_identity(alias, state_root=tmp_path)


def test_database_identity_still_refuses_an_outside_path(tmp_path):
    from reliable_memory import capture_operational_database_identity

    root = tmp_path / "vault"
    root.mkdir()
    outside = tmp_path / "database"
    outside.write_bytes(b"evidence")
    with pytest.raises((PermissionError, ValueError)):
        capture_operational_database_identity(outside, state_root=root)


def test_mutable_identity_still_refuses_a_replaced_file(tmp_path):
    path = tmp_path / "database"
    path.write_bytes(b"original")
    record = {"path": path.name, "identity": _identity_value(
        capture_runtime_file_identity(path, state_root=tmp_path)
    )}
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"replacement")
    replacement.replace(path)
    with pytest.raises(ValueError, match="artifact identity changed"):
        _adoption_check(record, path, tmp_path)
