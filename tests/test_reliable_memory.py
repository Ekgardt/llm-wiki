from __future__ import annotations

import contextlib
import errno
import os
import sqlite3
import stat
import subprocess
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path, PurePosixPath

import pytest
import reliable_memory
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
    assert sha256_bytes(b"") == ("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")


def test_begin_immediate_rolls_back_when_precommit_fence_expires():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE values_table(value INTEGER)")

    with pytest.raises(TimeoutError, match="deadline"):
        with begin_immediate(
            connection,
            before_commit=lambda: (_ for _ in ()).throw(
                TimeoutError("deadline expired before commit")
            ),
        ):
            connection.execute("INSERT INTO values_table VALUES (1)")

    assert connection.execute("SELECT value FROM values_table").fetchall() == []


def test_begin_immediate_commits_when_precommit_fence_succeeds():
    connection = sqlite3.connect(":memory:")
    checked = []
    connection.execute("CREATE TABLE values_table(value INTEGER)")

    with begin_immediate(connection, before_commit=lambda: checked.append(True)):
        connection.execute("INSERT INTO values_table VALUES (1)")

    assert checked == [True]
    assert connection.execute("SELECT value FROM values_table").fetchall() == [(1,)]


def test_begin_immediate_preserves_body_exception_when_rollback_fails():
    class BrokenRollbackConnection:
        def execute(self, statement):
            assert statement == "BEGIN IMMEDIATE"

        def commit(self):
            pytest.fail("commit must not run after a body failure")

        def rollback(self):
            raise OSError("rollback failed")

    original = RuntimeError("body failed")

    with pytest.raises(RuntimeError, match="body failed") as raised:
        with begin_immediate(BrokenRollbackConnection()):
            raise original

    assert raised.value is original
    assert isinstance(raised.value.__context__, OSError)


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
    with contextlib.closing(open_operational_db(path, busy_ms=10_000)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 10_000
        db.execute("CREATE TABLE sample (name TEXT)")
        db.execute("INSERT INTO sample VALUES ('row')")
        assert db.execute("SELECT name FROM sample").fetchone()["name"] == "row"

    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_begin_immediate_commits_and_rolls_back(tmp_path):
    with contextlib.closing(
        open_operational_db(tmp_path / "run" / "x.sqlite3", busy_ms=100)
    ) as db:
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync contract")
def test_directory_fsync_does_not_swallow_real_io_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(reliable_memory.os, "open", lambda _path, _flags: 123)
    monkeypatch.setattr(reliable_memory.os, "close", lambda _descriptor: None)
    monkeypatch.setattr(
        reliable_memory.os,
        "fsync",
        lambda _descriptor: (_ for _ in ()).throw(OSError(errno.EACCES, "denied")),
    )

    with pytest.raises(OSError) as exc_info:
        fsync_directory(tmp_path)

    assert exc_info.value.errno == errno.EACCES


@pytest.mark.skipif(os.name != "nt", reason="Windows directory flush contract")
def test_windows_directory_flush_failure_is_durability_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import windows_workspace

    closed = []
    monkeypatch.setattr(windows_workspace, "open_writable_directory_path", lambda _path: 91)
    monkeypatch.setattr(windows_workspace, "flush_directory", lambda _handle: False)
    monkeypatch.setattr(windows_workspace, "close_handle", closed.append)

    with pytest.raises(reliable_memory.MetadataDurabilityUnavailable) as raised:
        fsync_directory(tmp_path)

    assert raised.value.code == "metadata_durability_unavailable"
    assert closed == [91]


def test_metadata_durability_error_has_stable_code() -> None:
    error = reliable_memory.MetadataDurabilityUnavailable("flush failed")

    assert error.code == "metadata_durability_unavailable"


def test_durable_publish_reports_published_adopted_and_duplicate(tmp_path: Path) -> None:
    expected = b'{"value":1}'
    digest = sha256_bytes(expected)
    destination = tmp_path / "state.json"
    staged = tmp_path / "state.first.tmp"
    destination.write_bytes(b"old")
    staged.write_bytes(expected)

    assert reliable_memory.durable_publish_file(
        staged,
        destination,
        replace=True,
        expected_sha256=digest,
        max_bytes=len(expected),
    ) == "published"
    assert destination.read_bytes() == expected
    assert not staged.exists()

    assert reliable_memory.durable_publish_file(
        staged,
        destination,
        replace=True,
        expected_sha256=digest,
        max_bytes=len(expected),
    ) == "adopted"

    duplicate = tmp_path / "state.duplicate.tmp"
    duplicate.write_bytes(expected)
    assert reliable_memory.durable_publish_file(
        duplicate,
        destination,
        replace=True,
        expected_sha256=digest,
        max_bytes=len(expected),
    ) == "duplicate"
    assert duplicate.read_bytes() == expected
    assert destination.read_bytes() == expected


def test_runtime_file_identity_round_trips_through_create_only_publication(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    destination = state_root / "run" / "record.json"
    destination.parent.mkdir()

    published = reliable_memory.publish_runtime_file(
        destination,
        b'{"value":1}',
        state_root=state_root,
        create_only=True,
    )

    assert reliable_memory.capture_runtime_file_identity(
        destination, state_root=state_root
    ) == published
    assert published.size == len(b'{"value":1}')
    assert published.platform in {"posix", "windows"}
    assert published.volume
    assert published.file_id
    assert destination.read_bytes() == b'{"value":1}'


def test_runtime_file_publication_is_create_only_and_compare_and_replace_fenced(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    destination = state_root / "run" / "record.json"
    destination.parent.mkdir()
    original = reliable_memory.publish_runtime_file(
        destination,
        b"original",
        state_root=state_root,
        create_only=True,
    )

    with pytest.raises(FileExistsError):
        reliable_memory.publish_runtime_file(
            destination,
            b"different",
            state_root=state_root,
            create_only=True,
        )
    with pytest.raises(PermissionError, match="identity changed"):
        reliable_memory.publish_runtime_file(
            destination,
            b"replacement",
            state_root=state_root,
            create_only=False,
            expected=reliable_memory.RuntimeFileIdentity(
                platform=original.platform,
                volume=original.volume,
                file_id=original.file_id,
                size=original.size + 1,
                mtime_ns=original.mtime_ns,
            ),
            expected_sha256=sha256_bytes(b"original"),
        )

    replaced = reliable_memory.publish_runtime_file(
        destination,
        b"replacement",
        state_root=state_root,
        create_only=False,
        expected=original,
        expected_sha256=sha256_bytes(b"original"),
    )

    assert replaced != original
    assert destination.read_bytes() == b"replacement"


@pytest.mark.parametrize(
    ("staged_bytes", "destination_bytes"),
    [(None, None), (b"wrong", None), (None, b"wrong"), (b"wrong", b"wrong")],
)
def test_durable_publish_never_accepts_missing_or_wrong_bytes(
    tmp_path: Path,
    staged_bytes: bytes | None,
    destination_bytes: bytes | None,
) -> None:
    expected = b"expected"
    staged = tmp_path / "state.tmp"
    destination = tmp_path / "state.json"
    if staged_bytes is not None:
        staged.write_bytes(staged_bytes)
    if destination_bytes is not None:
        destination.write_bytes(destination_bytes)

    with pytest.raises(RuntimeError, match="publication conflict"):
        reliable_memory.durable_publish_file(
            staged,
            destination,
            replace=True,
            expected_sha256=sha256_bytes(expected),
            max_bytes=len(expected),
        )

    assert staged.read_bytes() == staged_bytes if staged_bytes is not None else not staged.exists()
    assert (
        destination.read_bytes() == destination_bytes
        if destination_bytes is not None
        else not destination.exists()
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync contract")
def test_posix_sync_failure_after_replace_is_retryable_as_adopted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = b"new"
    staged = tmp_path / "state.tmp"
    destination = tmp_path / "state.json"
    staged.write_bytes(expected)
    destination.write_bytes(b"old")
    real_fsync = reliable_memory.os.fsync

    def fail_directory(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "directory sync unsupported")
        real_fsync(descriptor)

    monkeypatch.setattr(reliable_memory.os, "fsync", fail_directory)
    with pytest.raises(reliable_memory.MetadataDurabilityUnavailable):
        reliable_memory.durable_publish_file(
            staged,
            destination,
            replace=True,
            expected_sha256=sha256_bytes(expected),
            max_bytes=len(expected),
        )
    assert not staged.exists()
    assert destination.read_bytes() == expected

    synced_directories = []

    def record_directory(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            synced_directories.append(os.fstat(descriptor).st_ino)
        real_fsync(descriptor)

    monkeypatch.setattr(reliable_memory.os, "fsync", record_directory)
    assert reliable_memory.durable_publish_file(
        staged,
        destination,
        replace=True,
        expected_sha256=sha256_bytes(expected),
        max_bytes=len(expected),
    ) == "adopted"
    assert synced_directories


@pytest.mark.skipif(os.name == "nt", reason="POSIX create-only publication contract")
def test_posix_unlink_failure_retains_observable_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = b"new"
    staged = tmp_path / "state.tmp"
    destination = tmp_path / "state.json"
    staged.write_bytes(expected)
    real_unlink = reliable_memory.os.unlink

    def fail_staged_unlink(path: object, *args: object, **kwargs: object) -> None:
        if Path(path) == staged:
            raise OSError(errno.EACCES, "cannot remove staged evidence")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(reliable_memory.os, "unlink", fail_staged_unlink)
    with pytest.raises(OSError, match="staged evidence"):
        reliable_memory.durable_publish_file(
            staged,
            destination,
            replace=False,
            expected_sha256=sha256_bytes(expected),
            max_bytes=len(expected),
        )
    assert staged.read_bytes() == expected
    assert destination.read_bytes() == expected

    assert reliable_memory.durable_publish_file(
        staged,
        destination,
        replace=False,
        expected_sha256=sha256_bytes(expected),
        max_bytes=len(expected),
    ) == "duplicate"


@pytest.mark.skipif(os.name != "nt", reason="Windows write-through publication contract")
@pytest.mark.parametrize("failure", ["flush", "move"])
def test_windows_publication_failure_keeps_old_and_staged_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    import windows_workspace

    expected = b"new"
    staged = tmp_path / "state.tmp"
    destination = tmp_path / "state.json"
    staged.write_bytes(expected)
    destination.write_bytes(b"old")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError(5, f"{failure} failed")

    monkeypatch.setattr(
        windows_workspace,
        "flush_file_path",
        fail if failure == "flush" else lambda _path: None,
    )
    monkeypatch.setattr(
        windows_workspace,
        "move_file_write_through",
        fail if failure == "move" else lambda *_args, **_kwargs: None,
    )

    with pytest.raises(reliable_memory.MetadataDurabilityUnavailable):
        reliable_memory.durable_publish_file(
            staged,
            destination,
            replace=True,
            expected_sha256=sha256_bytes(expected),
            max_bytes=len(expected),
        )
    assert staged.read_bytes() == expected
    assert destination.read_bytes() == b"old"


def test_state_root_accepts_normal_local_sqlite_locking(tmp_path):
    validate_state_root(tmp_path / "state")


def test_state_root_rejects_known_network_path(tmp_path, monkeypatch):
    monkeypatch.setattr("reliable_memory._known_network_path", lambda path: True)
    with pytest.raises(UnsafeStateRoot, match="local filesystem"):
        validate_state_root(tmp_path)


def test_posix_network_mount_detection_uses_longest_mount_point(monkeypatch):
    mount_data = """
36 25 0:32 / / rw,relatime - nfs4 server:/root rw
37 36 8:1 / /srv/local rw,relatime - ext4 /dev/sda1 rw
38 36 0:44 / /srv/network rw,relatime - cifs //server/share rw
"""
    monkeypatch.setattr(reliable_memory, "_platform_system", lambda: "Linux", raising=False)
    monkeypatch.setattr(
        reliable_memory, "_read_posix_mount_data", lambda: (mount_data, True), raising=False
    )
    monkeypatch.setattr(type(Path("/")), "resolve", lambda self, *, strict=False: self)
    assert reliable_memory._known_network_path(Path("/uncovered")) is True
    assert reliable_memory._known_network_path(Path("/srv/local/state")) is False
    assert reliable_memory._known_network_path(Path("/srv/network/state")) is True


def test_posix_mount_detection_resolves_symlink_target(monkeypatch):
    mount_data = """
36 25 8:1 / / rw,relatime - ext4 /dev/sda1 rw
37 36 0:44 / /mnt/network rw,relatime - nfs server:/share rw
"""
    link = Path("/local/state-link")
    calls = []

    def resolve(self, *, strict=False):
        calls.append((self, strict))
        return Path("/mnt/network/state")

    monkeypatch.setattr(reliable_memory, "_platform_system", lambda: "Linux")
    monkeypatch.setattr(reliable_memory, "_read_posix_mount_data", lambda: (mount_data, True))
    monkeypatch.setattr(type(link), "resolve", resolve)

    assert reliable_memory._known_network_path(link) is True
    assert calls == [(link, False)]


def test_posix_proc_mounts_network_types_are_detected(monkeypatch):
    mounts = "server:/nfs /mnt/nfs nfs rw 0 0\nsshfs /mnt/ssh fuse.sshfs rw 0 0\n"
    monkeypatch.setattr(reliable_memory, "_platform_system", lambda: "Linux", raising=False)
    monkeypatch.setattr(
        reliable_memory, "_read_posix_mount_data", lambda: (mounts, False), raising=False
    )
    monkeypatch.setattr(type(Path("/")), "resolve", lambda self, *, strict=False: self)
    assert reliable_memory._known_network_path(Path("/mnt/nfs/state")) is True
    assert reliable_memory._known_network_path(Path("/mnt/ssh/state")) is True


def test_darwin_mount_detection_handles_spaces_escapes_and_longest_match(monkeypatch):
    mount_output = """
/dev/disk3s1s1 on / (apfs, sealed, local, read-only)
server:/team on /Volumes/Team Share (nfs, nodev, nosuid)
/dev/disk4s1 on /Volumes/Team Share/local (apfs, local)
//user@server/share on /Volumes/SMB\\040Share (smbfs, nodev, nosuid)
"""
    monkeypatch.setattr(reliable_memory, "_platform_system", lambda: "Darwin")
    monkeypatch.setattr(reliable_memory, "_read_posix_mount_data", lambda: ("", True))
    monkeypatch.setattr(
        reliable_memory, "_query_darwin_mounts", lambda: mount_output, raising=False
    )
    monkeypatch.setattr(type(Path("/")), "resolve", lambda self, *, strict=False: self)

    assert reliable_memory._known_network_path(Path("/Volumes/Team Share/project")) is True
    assert reliable_memory._known_network_path(Path("/Volumes/Team Share/local/state")) is False
    assert reliable_memory._known_network_path(Path("/Volumes/SMB Share/state")) is True


def test_darwin_mount_query_timeout_fails_open_for_unknown_path(monkeypatch):
    monkeypatch.setattr(reliable_memory, "_platform_system", lambda: "Darwin")
    monkeypatch.setattr(reliable_memory, "_read_posix_mount_data", lambda: ("", True))

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(reliable_memory.subprocess, "run", timeout)
    assert reliable_memory._query_darwin_mounts() == ""
    assert reliable_memory._known_network_path(Path("/Users/local/state")) is False


def test_darwin_mount_query_is_bounded(monkeypatch):
    observed = {}

    def completed(command, **kwargs):
        observed.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(reliable_memory.subprocess, "run", completed)
    assert reliable_memory._query_darwin_mounts() == ""
    assert observed["timeout"] <= 2
    assert observed["check"] is False


def test_owner_permission_errors_are_not_suppressed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        reliable_memory, "_owner_permissions_supported", lambda path: True, raising=False
    )

    def deny_chmod(self, mode):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "chmod", deny_chmod)
    with pytest.raises(PermissionError, match="denied"):
        validate_state_root(tmp_path / "state")


def test_owner_mode_must_match_after_chmod(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir(mode=0o755)
    monkeypatch.setattr(
        reliable_memory, "_owner_permissions_supported", lambda path: True, raising=False
    )
    monkeypatch.setattr(Path, "chmod", lambda self, mode: None)
    with pytest.raises(PermissionError, match="owner-only"):
        validate_state_root(root)


def test_unsupported_owner_bits_skip_chmod_explicitly(tmp_path, monkeypatch):
    monkeypatch.setattr(
        reliable_memory, "_owner_permissions_supported", lambda path: False, raising=False
    )
    monkeypatch.setattr(
        Path,
        "chmod",
        lambda self, mode: pytest.fail("chmod must not run when modes are unsupported"),
    )
    monkeypatch.setattr(reliable_memory, "_sqlite_lock_probe", lambda path: True)
    validate_state_root(tmp_path / "state")


def test_state_root_rejects_windows_reparse_point(tmp_path, monkeypatch):
    monkeypatch.setattr("reliable_memory._windows_reparse_point", lambda path: True)
    monkeypatch.setattr(
        "reliable_memory._known_network_path",
        lambda path: pytest.fail("network probing must follow reparse rejection"),
    )
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


def test_concurrent_state_root_validation_uses_unique_probe_databases(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    connected_paths = []
    connected_paths_lock = threading.Lock()
    real_connect = sqlite3.connect

    def recording_connect(database, *args, **kwargs):
        with connected_paths_lock:
            connected_paths.append(Path(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(reliable_memory.sqlite3, "connect", recording_connect)
    monkeypatch.setattr(reliable_memory, "_known_network_path", lambda path: False)
    monkeypatch.setattr(reliable_memory, "_windows_reparse_point", lambda path: False)
    monkeypatch.setattr(reliable_memory, "_set_owner_only", lambda path, mode: True)
    with ThreadPoolExecutor(max_workers=24) as executor:
        results = list(executor.map(lambda _index: validate_state_root(root), range(64)))

    assert results == [None] * 64
    probes = {path for path in connected_paths if path.name.startswith(".llm-wiki-lock-probe-")}
    assert len(probes) == 64
    assert not list(root.glob(".llm-wiki-lock-probe-*"))


def _posix_locks_on_inode(inode: int) -> int:
    """Count kernel advisory locks recorded against one inode."""
    total = 0
    with open("/proc/locks", encoding="ascii") as records:
        for line in records:
            fields = line.split()
            if len(fields) >= 6 and fields[5].split(":")[-1] == str(inode):
                total += 1
    return total


@pytest.mark.skipif(
    not Path("/proc/locks").exists(), reason="kernel lock table is unavailable"
)
def test_reopening_an_operational_database_keeps_existing_locks(tmp_path: Path) -> None:
    """A second open must not strip the locks a live transaction depends on.

    `close()` releases every POSIX advisory lock the process holds on an inode,
    whichever descriptor took them, so any descriptor probe against a database
    file silently disarms SQLite's cross-process exclusion.
    """
    state_root = tmp_path / "state"
    state_root.mkdir()
    database = state_root / "operational.sqlite3"
    holder = open_operational_db(database, busy_ms=1_000)
    try:
        holder.execute("CREATE TABLE t(v TEXT)")
        with begin_immediate(holder):
            holder.execute("INSERT INTO t(v) VALUES ('holder')")
            inode = database.stat().st_ino
            before = _posix_locks_on_inode(inode)
            second = open_operational_db(database, busy_ms=1_000)
            second.close()
            after = _posix_locks_on_inode(inode)
    finally:
        holder.close()

    assert before > 0
    assert after == before
