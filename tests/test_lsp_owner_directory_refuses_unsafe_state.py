from __future__ import annotations

import os
import stat
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from llm_wiki import lsp_process_owner
from llm_wiki.lsp_process_limits import MAX_ACL_OUTPUT_BYTES
from llm_wiki.lsp_process_owner import ObjectIdentity, OwnerDirectory

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX owner directory semantics")

ACL_IDENTITY = "HOST\\alice"


def _created_owner(tmp_path: Path) -> OwnerDirectory:
    owner = OwnerDirectory.open(tmp_path / "run" / "owner-1")
    owner.create(time.monotonic() + 30)
    return owner


@pytest.fixture
def run_parent(tmp_path: Path) -> Path:
    parent = tmp_path / "run"
    parent.mkdir(mode=0o700)
    return parent


def _closed_after(owner: OwnerDirectory) -> None:
    owner.close()


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"x" * 5000, "exceeds its byte bound"),
        (b"\xff\xfe", "not canonical JSON"),
        (b"{not json", "not canonical JSON"),
        (b"[1,2]", "not a JSON object"),
        (b'{"b": 1, "a": 2}', "not canonical JSON"),
    ],
)
def test_a_record_is_read_only_in_its_bounded_canonical_form(
    run_parent: Path, payload: bytes, message: str
) -> None:
    owner = _created_owner(run_parent.parent)
    (run_parent / "owner-1" / "owner.json").write_bytes(payload)
    try:
        with pytest.raises(ValueError, match=message):
            owner.read_record("owner.json")
    finally:
        _closed_after(owner)


def test_a_record_that_is_not_a_regular_file_is_refused(run_parent: Path) -> None:
    owner = _created_owner(run_parent.parent)
    (run_parent / "owner-1" / "failure.json").mkdir()
    try:
        with pytest.raises(PermissionError, match="unsafe identity"):
            owner.read_record("failure.json")
    finally:
        _closed_after(owner)


def test_a_canonical_record_round_trips(run_parent: Path) -> None:
    owner = _created_owner(run_parent.parent)
    try:
        owner.write_record("owner.json", {"b": 1, "a": [2]})
        assert owner.read_record("owner.json") == {"a": [2], "b": 1}
        assert (run_parent / "owner-1" / "owner.json").read_bytes() == b'{"a":[2],"b":1}'
    finally:
        _closed_after(owner)


def test_records_and_leases_past_their_bound_are_refused_before_writing(
    run_parent: Path,
) -> None:
    owner = _created_owner(run_parent.parent)
    oversized = {"tail": "x" * 5000}
    try:
        with pytest.raises(ValueError, match="evidence record exceeds"):
            owner.write_record("failure.json", oversized)
        with pytest.raises(ValueError, match="lease exceeds"):
            owner.write_lease(oversized)
        assert sorted(path.name for path in (run_parent / "owner-1").iterdir()) == ["cancellation"]
    finally:
        _closed_after(owner)


def test_a_closed_owner_directory_refuses_leases_and_syncs(run_parent: Path) -> None:
    owner = _created_owner(run_parent.parent)
    owner.close()

    with pytest.raises(RuntimeError, match="closed"):
        owner.write_lease({"state": "live"})
    with pytest.raises(RuntimeError, match="closed"):
        owner.sync_directory()
    owner.remove_lease()
    owner.remove_success_scratch()
    assert (run_parent / "owner-1").is_dir()


def test_a_closed_owner_with_a_pending_temporary_cannot_forget_it(tmp_path: Path) -> None:
    owner = OwnerDirectory(
        tmp_path / "owner-1",
        parent_handle=-1,
        parent_identity=None,
        _pending_temp_names=[".lease-0.tmp"],
    )

    with pytest.raises(RuntimeError, match="pending temporary owner is closed"):
        owner.remove_lease()
    with pytest.raises(RuntimeError, match="pending temporary owner is closed"):
        owner.close()


def test_a_pending_temporary_name_is_held_once_and_within_its_bound(tmp_path: Path) -> None:
    owner = OwnerDirectory(tmp_path / "owner-1", parent_handle=-1, parent_identity=None)

    owner._remember_temp_name(".evidence-1.tmp")
    owner._remember_temp_name(".evidence-1.tmp")

    assert owner._pending_temp_names == [".evidence-1.tmp"]
    with pytest.raises(RuntimeError, match="temporary name bound was exhausted"):
        owner._remember_temp_name(".evidence-2.tmp")


def test_an_unsupported_platform_cannot_sync_or_clean_temporaries(
    run_parent: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _created_owner(run_parent.parent)
    owner._pending_temp_names.append(".lease-0.tmp")
    try:
        _refused_on_an_unsupported_platform(owner, monkeypatch)
    finally:
        owner._pending_temp_names.clear()
        _closed_after(owner)


def _refused_on_an_unsupported_platform(
    owner: OwnerDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(os, "name", "java")
        with pytest.raises(RuntimeError, match="unsupported on this platform"):
            owner.sync_directory()
        with pytest.raises(RuntimeError, match="unsupported on this platform"):
            owner.remove_lease()


def test_owner_directories_need_no_follow_directory_apis(
    run_parent: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(os, "O_NOFOLLOW")

    with pytest.raises(RuntimeError, match="no-follow directory APIs"):
        OwnerDirectory.open(run_parent / "owner-1")


def test_a_success_scratch_removal_leaves_a_replaced_owner_root_alone(run_parent: Path) -> None:
    owner = _created_owner(run_parent.parent)
    (run_parent / "owner-1").rename(run_parent / "owner-moved")
    (run_parent / "owner-1").mkdir()
    try:
        with pytest.raises(PermissionError, match="identity changed before deletion"):
            owner.remove_success_scratch()
        assert (run_parent / "owner-1").is_dir()
    finally:
        _closed_after(owner)


def test_a_launch_copy_that_vanished_is_not_one(run_parent: Path) -> None:
    descriptor = os.open(run_parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert not lsp_process_owner._is_launch_copy(descriptor, ".launch-gone")
    finally:
        os.close(descriptor)


def _move_parent(run_parent: Path) -> None:
    run_parent.rename(run_parent.parent / "run-moved")
    run_parent.mkdir()


def _remove_parent(run_parent: Path) -> None:
    run_parent.rename(run_parent.parent / "run-moved")


def _replace_owner_root(run_parent: Path) -> None:
    (run_parent / "owner-1").rename(run_parent / "owner-moved")
    (run_parent / "owner-1").mkdir()


def _loosen_owner_mode(run_parent: Path) -> None:
    (run_parent / "owner-1").chmod(0o750)


@pytest.mark.parametrize(
    ("change", "error", "message"),
    [
        (_move_parent, RuntimeError, "parent identity changed"),
        (_remove_parent, RuntimeError, "parent identity changed"),
        (_replace_owner_root, RuntimeError, "owner root identity changed"),
        (_loosen_owner_mode, PermissionError, "not owner-only"),
    ],
)
def test_the_lexical_identity_refuses_a_moved_or_loosened_owner_root(
    run_parent: Path,
    change: Callable[[Path], None],
    error: type[Exception],
    message: str,
) -> None:
    owner = _created_owner(run_parent.parent)
    change(run_parent)
    try:
        with pytest.raises(error, match=message):
            owner.verify_lexical_identity()
    finally:
        _closed_after(owner)


@pytest.mark.parametrize(
    ("file_type", "reparse", "message"),
    [
        (stat.S_IFLNK, 0, "symlink or reparse point"),
        (stat.S_IFDIR, 0x400, "symlink or reparse point"),
        (stat.S_IFREG, 0, "must be a directory"),
    ],
)
def test_a_directory_identity_must_be_a_plain_directory(
    file_type: int, reparse: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        lsp_process_owner._require_directory_identity(
            ObjectIdentity(1, 2, file_type, reparse), "owner_root parent"
        )


def test_a_descriptor_write_that_makes_no_progress_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = os.open(tmp_path / "record", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        _write_while_writes_make_no_progress(descriptor, monkeypatch)
    finally:
        os.close(descriptor)


def _write_while_writes_make_no_progress(descriptor: int, monkeypatch: pytest.MonkeyPatch) -> None:
    def write_nothing(target: int, data: bytes) -> int:
        return 0

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", write_nothing)
        with pytest.raises(OSError, match="made no progress"):
            lsp_process_owner.write_all_descriptor(descriptor, b"payload")


class AclTool:
    def __init__(self, listing: bytes, *failing_codes: int) -> None:
        self.listing = listing
        self.codes = list(failing_codes)
        self.commands: list[list[str]] = []

    def run(
        self,
        command: Sequence[str],
        *,
        shell: bool,
        check: bool,
        capture_output: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        self.commands.append(list(command))
        code = (self.codes or [0]).pop(0)
        return subprocess.CompletedProcess(list(command), code, self.listing, b"")


def _listing(entry: str, path: Path) -> bytes:
    return f"{path} {entry}\r\n\r\nSuccessfully processed 1 files\r\n".encode()


def _secure(monkeypatch: pytest.MonkeyPatch, tool: AclTool, path: Path, deadline: float) -> None:
    monkeypatch.setattr(lsp_process_owner, "windows_acl_identity", lambda: ACL_IDENTITY)
    monkeypatch.setattr(subprocess, "run", tool.run)
    lsp_process_owner.secure_windows_owner_root(path, deadline)


def test_an_owner_root_acl_is_narrowed_to_its_owner_and_verified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tool = AclTool(_listing(f"{ACL_IDENTITY}:(OI)(CI)(F)", tmp_path))

    _secure(monkeypatch, tool, tmp_path, time.monotonic() + 30)

    target = str(tmp_path)
    assert tool.commands == [
        ["icacls", target, "/inheritance:r", "/grant:r", f"{ACL_IDENTITY}:(OI)(CI)(F)"],
        ["icacls", target, "/remove:g", *lsp_process_owner.BROAD_ACL_SIDS],
        ["icacls", target, "/remove:d", *lsp_process_owner.BROAD_ACL_SIDS],
        ["icacls", target],
    ]


@pytest.mark.parametrize(
    ("entry", "codes", "message"),
    [
        (f"{ACL_IDENTITY}:(OI)(CI)(F)", (1,), "could not apply"),
        (f"{ACL_IDENTITY}:(OI)(CI)(F)", (0, 0, 0, 5), "could not verify"),
        ("HOST\\mallory:(OI)(CI)(F)", (), "verification failed"),
        (f"{ACL_IDENTITY}:(OI)(CI)(RX)", (), "verification failed"),
        (f"{ACL_IDENTITY}:(OI)(CI)(F)(N)", (), "verification failed"),
    ],
)
def test_an_owner_root_acl_that_is_not_owner_only_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    entry: str,
    codes: tuple[int, ...],
    message: str,
) -> None:
    tool = AclTool(_listing(entry, tmp_path), *codes)

    with pytest.raises(PermissionError, match=message):
        _secure(monkeypatch, tool, tmp_path, time.monotonic() + 30)


def test_an_owner_root_acl_past_its_deadline_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tool = AclTool(b"")

    with pytest.raises(PermissionError, match="deadline expired"):
        _secure(monkeypatch, tool, tmp_path, time.monotonic() - 1)
    assert tool.commands == []


@pytest.mark.parametrize(
    "failure",
    [OSError(2, "icacls not found"), subprocess.TimeoutExpired(["icacls"], 1.0)],
)
def test_an_acl_tool_that_cannot_run_is_a_permission_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: Exception
) -> None:
    def broken_run(command: Sequence[str], **options: object) -> object:
        raise failure

    monkeypatch.setattr(subprocess, "run", broken_run)

    with pytest.raises(PermissionError, match="ACL command failed") as raised:
        lsp_process_owner._run_windows_owner_acl(["icacls"], time.monotonic() + 30)
    assert raised.value.__cause__ is failure


def test_an_acl_tool_that_talks_too_much_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tool = AclTool(b"x" * (MAX_ACL_OUTPUT_BYTES + 1))

    with pytest.raises(PermissionError, match="output exceeded its byte bound"):
        _secure(monkeypatch, tool, tmp_path, time.monotonic() + 30)


def test_an_owner_directory_cannot_be_opened_on_an_unsupported_platform(
    run_parent: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "name", "java")

    with pytest.raises(RuntimeError, match="unsupported on this platform"):
        OwnerDirectory.open(run_parent / "owner-1")


def test_only_owner_and_failure_records_can_be_written_or_read(run_parent: Path) -> None:
    owner = _created_owner(run_parent.parent)
    try:
        with pytest.raises(ValueError, match="evidence name or owner handle is invalid"):
            owner.write_record("lease.json", {"a": 1})
        with pytest.raises(ValueError, match="evidence name or owner handle is invalid"):
            owner.read_record("../owner.json")
    finally:
        _closed_after(owner)
    assert not (run_parent / "owner-1" / "lease.json").exists()
