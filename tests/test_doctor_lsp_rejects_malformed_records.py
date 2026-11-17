from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import windows_workspace
from llm_wiki import doctor_lsp, monotonic_deadline
from windows_workspace import WindowsEntry

OWNER_NONCE = "a" * 32
GENERATION_NONCE = "b" * 32
OTHER_NONCE = "c" * 32
NOW = datetime(2026, 11, 17, 9, tzinfo=UTC)
OWNER_ID = b"o" * 16
CANCELLATION_ID = b"c" * 16
RECORD_ID = b"r" * 16
MOVED_ID = b"x" * 16
RECORD_NAMES = frozenset({"owner.json", "lease.json", "failure.json"})
POSIX_ONLY = pytest.mark.skipif(
    not Path("/proc/self/fd").is_dir(), reason="needs POSIX descriptors in /proc"
)


def _owner() -> dict[str, Any]:
    return {
        "command_basename": "pyright-langserver",
        "generation_nonce": GENERATION_NONCE,
        "owner_nonce": OWNER_NONCE,
        "owner_pid": 100,
        "started_at": "2026-11-17T08:00:00Z",
        "state": "process_running",
    }


def _lease() -> dict[str, Any]:
    return {
        "expires_at": "2026-11-17T08:00:40Z",
        "generation_nonce": GENERATION_NONCE,
        "heartbeat_at": "2026-11-17T08:00:10Z",
        "manager_pid": 100,
        "owner_nonce": OWNER_NONCE,
        "schema_version": 1,
        "server_pid": 101,
        "state": "live",
    }


def _failure() -> dict[str, Any]:
    return {
        "code": "process_exited",
        "generation_nonce": GENERATION_NONCE,
        "owner_nonce": OWNER_NONCE,
        "timestamp": "2026-11-17T08:00:20Z",
        "server_pid": 101,
        "stderr_tail": "bye",
    }


def _records() -> dict[str, dict[str, Any]]:
    return {"owner": _owner(), "lease": _lease(), "failure": _failure()}


def _snapshot(records: dict[str, dict[str, Any]]) -> tuple[object, ...]:
    return (
        OWNER_NONCE,
        RECORD_NAMES,
        records["owner"],
        records["lease"],
        records["failure"],
    )


def _dead(pid: object, identity: object) -> str:
    return "dead"


def _unanswerable(pid: object, identity: object) -> str:
    raise RuntimeError("process table unavailable")


class _DeadlineAfter:
    def __init__(self, allowed: int) -> None:
        self.allowed = allowed
        self.calls = 0

    def __call__(self, deadline: float) -> bool:
        self.calls += 1
        return self.calls > self.allowed


def test_well_formed_records_of_dead_processes_prove_death(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor_lsp, "lsp_pid_state", _dead)

    reading = doctor_lsp.read_lsp_owner(_snapshot(_records()), NOW, float("inf"))

    assert reading.unreadable is False
    assert reading.record["processes_dead"] is True
    assert "lsp_failure_evidence_retained" in reading.codes


@pytest.mark.parametrize(
    ("record", "field", "value"),
    [
        ("owner", "started_at", 5),
        ("owner", "started_at", "2026-13-01T00:00:00Z"),
        ("owner", "command_basename", ""),
        ("owner", "command_basename", "bin/pyright"),
        ("owner", "owner_nonce", OTHER_NONCE),
        ("owner", "generation_nonce", 7),
        ("owner", "owner_pid", 0),
        ("lease", "generation_nonce", 7),
        ("lease", "heartbeat_at", "soon"),
        ("lease", "state", "stale"),
        ("failure", "code", 5),
        ("failure", "code", "BAD"),
    ],
)
def test_a_malformed_record_leaves_death_unproven(
    monkeypatch: pytest.MonkeyPatch, record: str, field: str, value: object
) -> None:
    monkeypatch.setattr(doctor_lsp, "lsp_pid_state", _dead)
    records = _records()
    records[record][field] = value

    reading = doctor_lsp.read_lsp_owner(_snapshot(records), NOW, float("inf"))

    assert reading.unreadable is True
    assert reading.record["processes_dead"] is False


def test_a_process_table_that_cannot_answer_leaves_death_unproven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor_lsp, "lsp_pid_state", _unanswerable)

    reading = doctor_lsp.read_lsp_owner(_snapshot(_records()), NOW, float("inf"))

    assert reading.unreadable is True
    assert reading.record["processes_dead"] is False


@pytest.mark.parametrize(
    "lease",
    [{"owner_nonce": OWNER_NONCE}, {"owner_nonce": OTHER_NONCE, "heartbeat_at": "x"}],
)
def test_a_lease_that_does_not_match_its_owner_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, lease: dict[str, Any]
) -> None:
    monkeypatch.setattr(doctor_lsp, "lsp_pid_state", _dead)

    liveness = doctor_lsp.lsp_liveness(_owner(), lease, OWNER_NONCE, NOW, float("inf"))

    assert liveness.unreadable is True
    assert liveness.live is False


def test_an_owner_scan_past_its_deadline_stops_before_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(monotonic_deadline, "deadline_reached", _DeadlineAfter(0))

    assert doctor_lsp.scan_lsp_owners([_snapshot(_records())], NOW, 1.0) == ([], [], True)


def test_a_changed_observation_database_is_refused() -> None:
    with pytest.raises(ValueError, match="observation database changed"):
        doctor_lsp._require_observation_database((1,), (2,))


def test_a_json_array_is_not_a_runtime_record() -> None:
    with pytest.raises(ValueError, match="must be a JSON object"):
        doctor_lsp.decode_lsp_record(b"[]")


def test_directory_handles_need_no_follow_support(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)

    with pytest.raises(OSError, match="no-follow directory handles"):
        doctor_lsp._lsp_posix_directory_flags()


def test_a_windows_runtime_is_read_through_the_windows_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected: tuple[list[Any], bool, bool] = ([], False, True)
    monkeypatch.setattr(doctor_lsp, "snapshot_windows_lsp", lambda root, deadline: expected)
    monkeypatch.setattr(os, "name", "nt")

    assert doctor_lsp.snapshot_lsp_runtime(tmp_path, float("inf")) is expected


def test_an_unknown_platform_runtime_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "name", "java")

    assert doctor_lsp.snapshot_lsp_runtime(tmp_path, float("inf")) == ([], True, False)


def _posix_owner_tree(state_root: Path, owner_payload: bytes) -> None:
    owner = state_root / "run" / "lsp" / OWNER_NONCE
    (owner / "cancellation").mkdir(parents=True)
    (owner / "owner.json").write_bytes(owner_payload)


def _descriptor_target(name: str) -> str:
    try:
        return os.readlink(f"/proc/self/fd/{name}")
    except FileNotFoundError:
        return ""


def _descriptors_under(root: Path) -> list[str]:
    targets = (_descriptor_target(name) for name in os.listdir("/proc/self/fd"))
    return [target for target in targets if target.startswith(str(root))]


@POSIX_ONLY
def test_a_posix_owner_with_its_records_is_readable(tmp_path: Path) -> None:
    _posix_owner_tree(tmp_path, json.dumps(_owner()).encode())

    snapshots, unreadable, absent = doctor_lsp.snapshot_posix_lsp(tmp_path, float("inf"))

    assert (unreadable, absent) == (False, False)
    assert snapshots[0][2] == _owner()


@POSIX_ONLY
def test_an_oversized_posix_record_is_unreadable(tmp_path: Path) -> None:
    _posix_owner_tree(tmp_path, b" " * (doctor_lsp._LSP_RECORD_BYTES + 1))

    snapshots, unreadable, _absent = doctor_lsp.snapshot_posix_lsp(tmp_path, float("inf"))

    assert unreadable is True
    assert snapshots[0][2] is None


def _posix_deadline_calls(state_root: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    counter = _DeadlineAfter(1 << 30)
    monkeypatch.setattr(monotonic_deadline, "deadline_reached", counter)
    assert doctor_lsp.snapshot_posix_lsp(state_root, 1.0)[1] is False
    return counter.calls


@POSIX_ONLY
def test_a_posix_scan_cut_by_its_deadline_at_any_step_is_unreadable_and_leaks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _posix_owner_tree(tmp_path, json.dumps(_owner()).encode())
    total = _posix_deadline_calls(tmp_path, monkeypatch)

    for allowed in range(total):
        monkeypatch.setattr(monotonic_deadline, "deadline_reached", _DeadlineAfter(allowed))
        assert doctor_lsp.snapshot_posix_lsp(tmp_path, 1.0)[1] is True

    assert _descriptors_under(tmp_path) == []


class _Workspace:
    def __init__(self) -> None:
        self.root_entries = [WindowsEntry(OWNER_NONCE, "directory", OWNER_ID)]
        self.owner_entries = [
            WindowsEntry("cancellation", "directory", CANCELLATION_ID),
            WindowsEntry("owner.json", "file", RECORD_ID, 2),
        ]
        self.identities = {20: OWNER_ID, 30: CANCELLATION_ID, 40: RECORD_ID}
        self.size = 2
        self.payload = [b"{}"]
        self.opened: list[int] = []
        self.closed: list[int] = []

    def _open(self, handle: int) -> int:
        self.opened.append(handle)
        return handle

    def open_directory_path(self, path: Path) -> int:
        return self._open(10)

    def open_directory(self, parent: int, name: str) -> int:
        return self._open({10: 20, 20: 30}[parent])

    def open_file(self, parent: int, name: str) -> int:
        return self._open(40)

    def list_directory(self, handle: int, *, max_entries: int) -> list[WindowsEntry]:
        return {10: self.root_entries, 20: self.owner_entries}[handle]

    def identity(self, handle: int, *, directory: bool) -> tuple[int, bytes, bool]:
        return 1, self.identities[handle], directory

    def file_size(self, handle: int) -> int:
        return self.size

    def read_chunks(self, handle: int, *, chunk_bytes: int, max_bytes: int) -> list[bytes]:
        return self.payload

    def close_handle(self, handle: int) -> None:
        self.closed.append(handle)


def _install(monkeypatch: pytest.MonkeyPatch, workspace: _Workspace) -> None:
    for name in (
        "open_directory_path",
        "open_directory",
        "open_file",
        "list_directory",
        "identity",
        "file_size",
        "read_chunks",
        "close_handle",
    ):
        monkeypatch.setattr(windows_workspace, name, getattr(workspace, name))


def _owner_is_a_file(workspace: _Workspace) -> None:
    workspace.root_entries = [WindowsEntry(OWNER_NONCE, "file", OWNER_ID)]


def _owner_holds_an_unknown_child(workspace: _Workspace) -> None:
    workspace.owner_entries.append(WindowsEntry("extra.txt", "file", MOVED_ID))


def _cancellation_is_a_file(workspace: _Workspace) -> None:
    workspace.owner_entries[0] = WindowsEntry("cancellation", "file", CANCELLATION_ID)


def _cancellation_moved(workspace: _Workspace) -> None:
    workspace.identities[30] = MOVED_ID


def _owner_moved(workspace: _Workspace) -> None:
    workspace.identities[20] = MOVED_ID


def _record_has_no_identity(workspace: _Workspace) -> None:
    workspace.identities[40] = bytes(16)


def _record_moved(workspace: _Workspace) -> None:
    workspace.identities[40] = MOVED_ID


def _record_resized_before_read(workspace: _Workspace) -> None:
    workspace.size = 3


def _record_shortened_during_read(workspace: _Workspace) -> None:
    workspace.payload = [b"{"]


def _record_oversized(workspace: _Workspace) -> None:
    oversized = doctor_lsp._LSP_RECORD_BYTES + 1
    workspace.owner_entries[1] = WindowsEntry("owner.json", "file", RECORD_ID, oversized)


def test_a_windows_owner_with_its_records_is_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _Workspace()
    _install(monkeypatch, workspace)

    snapshots, unreadable, absent = doctor_lsp.snapshot_windows_lsp(Path("/v"), float("inf"))

    assert (unreadable, absent) == (False, False)
    assert snapshots[0][2] == {}
    assert sorted(workspace.closed) == sorted(workspace.opened)


@pytest.mark.parametrize(
    "damage",
    [
        _owner_is_a_file,
        _owner_holds_an_unknown_child,
        _cancellation_is_a_file,
        _cancellation_moved,
        _owner_moved,
        _record_has_no_identity,
        _record_moved,
        _record_resized_before_read,
        _record_shortened_during_read,
        _record_oversized,
    ],
)
def test_a_damaged_windows_owner_is_unreadable_and_closes_its_handles(
    monkeypatch: pytest.MonkeyPatch, damage: Callable[[_Workspace], None]
) -> None:
    workspace = _Workspace()
    damage(workspace)
    _install(monkeypatch, workspace)

    _snapshots, unreadable, absent = doctor_lsp.snapshot_windows_lsp(Path("/v"), float("inf"))

    assert (unreadable, absent) == (True, False)
    assert sorted(workspace.closed) == sorted(workspace.opened)


def _missing_root(path: Path) -> int:
    raise FileNotFoundError(path)


def test_a_missing_windows_runtime_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _Workspace())
    monkeypatch.setattr(windows_workspace, "open_directory_path", _missing_root)

    assert doctor_lsp.snapshot_windows_lsp(Path("/v"), float("inf")) == ([], False, True)


def _windows_deadline_calls(monkeypatch: pytest.MonkeyPatch) -> int:
    counter = _DeadlineAfter(1 << 30)
    monkeypatch.setattr(monotonic_deadline, "deadline_reached", counter)
    _install(monkeypatch, _Workspace())
    assert doctor_lsp.snapshot_windows_lsp(Path("/v"), 1.0)[1] is False
    return counter.calls


def test_a_windows_scan_cut_by_its_deadline_at_any_step_is_unreadable_and_closes_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    total = _windows_deadline_calls(monkeypatch)

    for allowed in range(total):
        workspace = _Workspace()
        _install(monkeypatch, workspace)
        monkeypatch.setattr(monotonic_deadline, "deadline_reached", _DeadlineAfter(allowed))
        assert doctor_lsp.snapshot_windows_lsp(Path("/v"), 1.0)[1] is True
        assert sorted(workspace.closed) == sorted(workspace.opened)
