from __future__ import annotations

import itertools
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import windows_workspace
from llm_wiki import lsp_process_owner
from llm_wiki.lsp_process_limits import MAX_PENDING_CHILD_HANDLES
from llm_wiki.lsp_process_owner import OwnerDirectory

from tests.slow_machine import LONG_TIMEOUT, SHORT_TIMEOUT

_IDS = itertools.count(1)


@dataclass(eq=False)
class FakeEntry:
    directory: bool
    data: bytearray = field(default_factory=bytearray)
    children: dict[str, FakeEntry] = field(default_factory=dict)
    file_id: int = field(default_factory=lambda: next(_IDS))


@dataclass(eq=False)
class FakeHandle:
    entry: FakeEntry
    parent: FakeEntry | None
    name: str
    delete_on_close: bool = False


class FakeWindowsVolume:
    def __init__(self, parent_path: Path) -> None:
        self.parent = FakeEntry(directory=True)
        self.roots = {parent_path: self.parent}
        self.handles: dict[int, FakeHandle] = {}
        self.next_handle = itertools.count(1000)
        self.failures: dict[str, list[BaseException]] = {}
        self.flushes_succeed = True
        self.calls: list[str] = []

    def fail(self, operation: str, *errors: BaseException) -> None:
        self.failures.setdefault(operation, []).extend(errors)

    def _step(self, operation: str) -> None:
        self.calls.append(operation)
        pending = self.failures.get(operation)
        if pending:
            raise pending.pop(0)

    def _open(self, entry: FakeEntry, parent: FakeEntry | None, name: str) -> int:
        handle = next(self.next_handle)
        self.handles[handle] = FakeHandle(entry, parent, name)
        return handle

    def _existing(self, parent: int, name: str, directory: bool) -> int:
        holder = self.handles[parent].entry
        entry = holder.children.get(name)
        if entry is None or entry.directory is not directory:
            raise FileNotFoundError(name)
        return self._open(entry, holder, name)

    def _created(self, parent: int, name: str, directory: bool) -> int:
        holder = self.handles[parent].entry
        if name in holder.children:
            raise FileExistsError(name)
        entry = FakeEntry(directory=directory)
        holder.children[name] = entry
        return self._open(entry, holder, name)

    def entry_names(self, entry: FakeEntry) -> set[str]:
        return set(entry.children)

    def owner_entry(self, name: str) -> FakeEntry:
        return self.parent.children[name]

    def identity(self, handle: int, *, directory: bool | None = None) -> tuple[int, bytes, bool]:
        self._step("identity")
        entry = self.handles[handle].entry
        return (1, entry.file_id.to_bytes(8, "little"), entry.directory)

    def open_directory_path(self, path: Path) -> int:
        self._step("open_directory_path")
        return self._open(self.roots[path], None, path.name)

    def create_writable_directory(self, parent: int, name: str) -> int:
        self._step("create_writable_directory")
        return self._created(parent, name, True)

    def create_directory(self, parent: int, name: str) -> int:
        self._step("create_directory")
        return self._created(parent, name, True)

    def open_directory(self, parent: int, name: str) -> int:
        self._step("open_directory")
        return self._existing(parent, name, True)

    def open_deletable_directory(self, parent: int, name: str) -> int:
        self._step("open_deletable_directory")
        return self._existing(parent, name, True)

    def create_file(self, parent: int, name: str) -> int:
        self._step("create_file")
        return self._created(parent, name, False)

    def open_file(self, parent: int, name: str) -> int:
        self._step("open_file")
        return self._existing(parent, name, False)

    def open_deletable_file(self, parent: int, name: str) -> int:
        self._step("open_deletable_file")
        return self._existing(parent, name, False)

    def write_all(self, handle: int, content: bytes, *, chunk_bytes: int) -> None:
        self._step("write_all")
        self.handles[handle].entry.data.extend(content)

    def flush_file(self, handle: int) -> None:
        self._step("flush_file")

    def flush_directory(self, handle: int) -> bool:
        self._step("flush_directory")
        return self.flushes_succeed

    def read_chunks(self, handle: int, *, chunk_bytes: int, max_bytes: int) -> Iterator[bytes]:
        self._step("read_chunks")
        data = bytes(self.handles[handle].entry.data)
        if len(data) > max_bytes:
            raise ValueError("file exceeds its byte bound")
        return iter(
            [data[offset : offset + chunk_bytes] for offset in range(0, len(data), chunk_bytes)]
        )

    def _rename(self, handle: int, parent: int, name: str, *, replace: bool) -> None:
        record = self.handles[handle]
        target = self.handles[parent].entry
        if name in target.children and not replace:
            raise FileExistsError(name)
        if record.parent is not None:
            del record.parent.children[record.name]
        target.children[name] = record.entry
        record.parent, record.name = target, name

    def publish_file(self, handle: int, parent: int, name: str) -> None:
        self._step("publish_file")
        self._rename(handle, parent, name, replace=False)

    def replace_file(self, handle: int, parent: int, name: str) -> None:
        self._step("replace_file")
        self._rename(handle, parent, name, replace=True)

    def delete_handle(self, handle: int) -> None:
        self._step("delete_handle")
        self.handles[handle].delete_on_close = True

    def close_handle(self, handle: int) -> None:
        self._step("close_handle")
        record = self.handles.pop(handle)
        if record.delete_on_close and record.parent is not None:
            record.parent.children.pop(record.name, None)


_PATCHED_OPERATIONS = (
    "identity",
    "open_directory_path",
    "create_writable_directory",
    "create_directory",
    "open_directory",
    "open_deletable_directory",
    "create_file",
    "open_file",
    "open_deletable_file",
    "write_all",
    "flush_file",
    "flush_directory",
    "read_chunks",
    "publish_file",
    "replace_file",
    "delete_handle",
    "close_handle",
)


def _owner_only_acl_applied(path: Path, deadline: float) -> None:
    return None


@pytest.fixture
def volume(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeWindowsVolume:
    fake = FakeWindowsVolume(tmp_path)
    for operation in _PATCHED_OPERATIONS:
        monkeypatch.setattr(windows_workspace, operation, getattr(fake, operation))
    monkeypatch.setattr(lsp_process_owner, "secure_windows_owner_root", _owner_only_acl_applied)
    monkeypatch.setattr(os, "name", "nt")
    return fake


def _deadline() -> float:
    return time.monotonic() + LONG_TIMEOUT


def _created_owner(tmp_path: Path) -> OwnerDirectory:
    owner = OwnerDirectory.open(tmp_path / "owner-1")
    owner.create(_deadline())
    return owner


def _lease_names(volume: FakeWindowsVolume) -> set[str]:
    return volume.entry_names(volume.owner_entry("owner-1"))


def test_an_owner_root_is_created_with_its_cancellation_directory(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)

    assert owner.owner_permissions_verified
    assert _lease_names(volume) == {"cancellation"}
    assert len(volume.handles) == 2


def test_opening_a_parent_whose_identity_cannot_be_read_closes_its_handle(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    volume.fail("identity", OSError(5, "access denied"))

    with pytest.raises(OSError, match="access denied"):
        OwnerDirectory.open(tmp_path / "owner-1")
    assert volume.handles == {}


def test_an_owner_root_that_moves_during_acl_setup_is_refused(
    volume: FakeWindowsVolume, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def swap_identity(path: Path, deadline: float) -> None:
        volume.owner_entry(path.name).file_id = next(_IDS)

    monkeypatch.setattr(lsp_process_owner, "secure_windows_owner_root", swap_identity)
    owner = OwnerDirectory.open(tmp_path / "owner-1")

    with pytest.raises(PermissionError, match="owner root identity changed during ACL"):
        owner.create(_deadline())
    assert not owner.owner_permissions_verified


def test_a_cancellation_directory_that_moves_is_refused_and_closed(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = OwnerDirectory.open(tmp_path / "owner-1")
    original_identity = volume.identity
    reads = itertools.count()

    def moving_identity(handle: int, *, directory: bool | None = None) -> tuple[int, bytes, bool]:
        found = original_identity(handle, directory=directory)
        if volume.handles[handle].name == "cancellation" and next(reads) == 1:
            return (found[0], b"moved", found[2])
        return found

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(windows_workspace, "identity", moving_identity)
        with pytest.raises(PermissionError, match="cancellation root identity changed"):
            owner.create(_deadline())
    assert len(volume.handles) == 2


def test_a_record_is_published_under_its_name_and_reads_back(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)

    owner.write_record("owner.json", {"owner_nonce": "n", "pid": 7})

    assert _lease_names(volume) == {"cancellation", "owner.json"}
    assert owner.read_record("owner.json") == {"owner_nonce": "n", "pid": 7}
    assert len(volume.handles) == 2


def test_a_record_needs_a_verified_owner_acl(volume: FakeWindowsVolume, tmp_path: Path) -> None:
    owner = _created_owner(tmp_path)
    owner.owner_permissions_verified = False

    with pytest.raises(PermissionError, match="ACL was not verified"):
        owner.write_record("owner.json", {"a": 1})


def test_a_record_whose_temporary_cannot_be_created_leaves_nothing_pending(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    volume.fail("create_file", OSError(112, "disk full"))

    with pytest.raises(OSError, match="disk full"):
        owner.write_record("owner.json", {"a": 1})
    assert owner._pending_temp_names == []
    assert _lease_names(volume) == {"cancellation"}


def test_a_record_that_moves_while_written_is_not_published(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)

    def move_after_flush(handle: int) -> None:
        volume.handles[handle].entry.file_id = next(_IDS)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(windows_workspace, "flush_file", move_after_flush)
        with pytest.raises(PermissionError, match="evidence identity changed"):
            owner.write_record("failure.json", {"a": 1})
    assert _lease_names(volume) == {"cancellation"}
    assert owner._pending_temp_names == []


def test_a_record_is_never_published_over_an_existing_one(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    owner.write_record("owner.json", {"first": True})

    with pytest.raises(FileExistsError):
        owner.write_record("owner.json", {"second": True})
    assert owner.read_record("owner.json") == {"first": True}
    assert _lease_names(volume) == {"cancellation", "owner.json"}


def test_a_leftover_temporary_that_could_not_be_deleted_is_removed_by_the_next_write(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    volume.fail("publish_file", OSError(5, "publication refused"))
    volume.fail("delete_handle", OSError(32, "sharing violation"))

    with pytest.raises(OSError, match="sharing violation") as raised:
        owner.write_record("owner.json", {"a": 1})
    assert "publication refused" in str(raised.value.__cause__)
    leftover = list(owner._pending_temp_names)
    assert len(leftover) == 1
    assert leftover[0] in _lease_names(volume)

    owner.write_record("owner.json", {"a": 1})

    assert owner._pending_temp_names == []
    assert _lease_names(volume) == {"cancellation", "owner.json"}


def test_a_temporary_handle_that_will_not_close_is_closed_by_the_next_operation(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    volume.fail("publish_file", OSError(5, "publication refused"))
    volume.fail("close_handle", OSError(6, "invalid handle"))

    with pytest.raises(OSError, match="invalid handle") as raised:
        owner.write_record("owner.json", {"a": 1})
    assert "publication refused" in str(raised.value.__cause__)
    assert len(owner._pending_child_handles) == 1

    owner.write_record("owner.json", {"a": 1})

    assert owner._pending_child_handles == []
    assert owner._pending_temp_names == []
    assert owner.read_record("owner.json") == {"a": 1}


def test_a_close_failure_after_a_failed_cleanup_names_the_cleanup_as_its_cause(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    volume.fail("publish_file", OSError(5, "publication refused"))
    volume.fail("delete_handle", OSError(32, "sharing violation"))
    volume.fail("close_handle", OSError(6, "invalid handle"))

    with pytest.raises(OSError, match="invalid handle") as raised:
        owner.write_record("owner.json", {"a": 1})
    assert "sharing violation" in str(raised.value.__cause__)


def test_a_published_record_whose_handle_will_not_close_reports_the_close(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    volume.fail("close_handle", OSError(6, "invalid handle"))

    with pytest.raises(OSError, match="invalid handle"):
        owner.write_record("owner.json", {"a": 1})
    assert "owner.json" in _lease_names(volume)
    assert owner._pending_temp_names == []


def test_a_pending_handle_that_still_will_not_close_blocks_the_next_write(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    volume.fail("close_handle", OSError(6, "invalid handle"), OSError(6, "still invalid"))

    with pytest.raises(OSError, match="invalid handle"):
        owner.write_record("owner.json", {"a": 1})
    with pytest.raises(OSError, match="still invalid"):
        owner.write_record("failure.json", {"a": 1})
    assert len(owner._pending_child_handles) == 1


def test_an_unflushable_owner_directory_is_reported(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    volume.flushes_succeed = False

    with pytest.raises(OSError, match="durability flush failed"):
        owner.sync_directory()


def test_a_record_read_through_a_handle_is_bounded_and_closed(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    oversized = volume.create_file(owner.owner_handle or 0, "owner.json")
    volume.write_all(oversized, b"x" * 5000, chunk_bytes=4096)
    volume.close_handle(oversized)

    with pytest.raises(ValueError, match="byte bound"):
        owner.read_record("owner.json")
    assert len(volume.handles) == 2


def _write_lease(owner: OwnerDirectory, generation: str, stop: threading.Event | None) -> None:
    now = time.monotonic()
    owner.write_lease(
        {"generation": generation},
        deadline=now + SHORT_TIMEOUT,
        expires_monotonic=now + LONG_TIMEOUT,
        retry_stop=stop,
    )


def _lease_record(volume: FakeWindowsVolume) -> object:
    return json.loads(bytes(volume.owner_entry("owner-1").children["lease.json"].data))


def test_a_lease_replaces_the_previous_one(volume: FakeWindowsVolume, tmp_path: Path) -> None:
    owner = _created_owner(tmp_path)

    _write_lease(owner, "first", None)
    _write_lease(owner, "second", None)

    assert _lease_record(volume) == {"generation": "second"}
    assert _lease_names(volume) == {"cancellation", "lease.json"}
    assert owner._lease_expires_monotonic is not None


def test_a_lease_held_open_by_a_reader_is_retried_until_it_can_be_replaced(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    volume.fail("replace_file", OSError(32, "lease reader is still open"))

    _write_lease(owner, "live", threading.Event())

    assert volume.calls.count("replace_file") == 2
    assert _lease_record(volume) == {"generation": "live"}


@pytest.mark.parametrize(
    ("error_code", "stop"),
    [(87, threading.Event()), (32, None)],
)
def test_a_lease_replace_that_cannot_be_waited_out_is_reported_and_cleaned_up(
    volume: FakeWindowsVolume, tmp_path: Path, error_code: int, stop: threading.Event | None
) -> None:
    owner = _created_owner(tmp_path)
    volume.fail("replace_file", OSError(error_code, "replace refused"))

    with pytest.raises(OSError, match="replace refused"):
        _write_lease(owner, "live", stop)
    assert _lease_names(volume) == {"cancellation"}
    assert owner._pending_temp_names == []


def test_a_lease_retry_stops_when_asked_to(volume: FakeWindowsVolume, tmp_path: Path) -> None:
    owner = _created_owner(tmp_path)
    volume.fail("replace_file", OSError(32, "lease reader is still open"))
    stop = threading.Event()
    stop.set()

    with pytest.raises(OSError, match="still open"):
        _write_lease(owner, "live", stop)
    assert volume.calls.count("replace_file") == 1


def test_a_lease_retry_never_outlives_the_previous_lease(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    owner.write_lease({"generation": "old"}, expires_monotonic=time.monotonic() - 1)
    volume.fail("replace_file", OSError(33, "region locked"))

    with pytest.raises(OSError, match="region locked"):
        _write_lease(owner, "new", threading.Event())
    assert _lease_record(volume) == {"generation": "old"}


def test_a_lease_whose_temporary_cannot_be_created_is_reported(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    volume.fail("create_file", OSError(112, "disk full"))

    with pytest.raises(OSError, match="disk full"):
        _write_lease(owner, "live", None)
    assert owner._pending_temp_names == []


def test_removing_the_lease_deletes_it_and_tolerates_its_absence(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    _write_lease(owner, "live", None)

    owner.remove_lease()
    owner.remove_lease()

    assert _lease_names(volume) == {"cancellation"}
    assert owner._lease_expires_monotonic is None


def test_a_lease_that_cannot_be_deleted_still_has_its_handle_closed(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    _write_lease(owner, "live", None)
    volume.fail("delete_handle", OSError(5, "delete refused"))

    with pytest.raises(OSError, match="delete refused"):
        owner.remove_lease()
    assert "lease.json" in _lease_names(volume)
    assert len(volume.handles) == 2


def test_a_lease_delete_and_close_failing_together_report_the_delete(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    _write_lease(owner, "live", None)
    volume.fail("delete_handle", OSError(5, "delete refused"))
    volume.fail("close_handle", OSError(6, "invalid handle"))

    with pytest.raises(OSError, match="delete refused") as raised:
        owner.remove_lease()
    assert "invalid handle" in str(raised.value.__cause__)
    assert len(owner._pending_child_handles) == 1


def test_a_lease_whose_close_fails_after_deletion_reports_the_close(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    _write_lease(owner, "live", None)
    volume.fail("close_handle", OSError(6, "invalid handle"))

    with pytest.raises(OSError, match="invalid handle"):
        owner.remove_lease()


def test_a_successful_run_removes_its_whole_owner_root(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    owner.write_record("owner.json", {"a": 1})
    owner.write_record("failure.json", {"b": 2})

    owner.remove_success_scratch()
    owner.close()

    assert "owner-1" not in volume.parent.children
    assert owner.owner_handle is None
    assert volume.handles == {}


def test_scratch_removal_tolerates_an_already_missing_owner_root(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    original_open = volume.open_deletable_directory

    def owner_root_gone(parent: int, name: str) -> int:
        if name == "owner-1":
            raise FileNotFoundError(name)
        return original_open(parent, name)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(windows_workspace, "open_deletable_directory", owner_root_gone)
        owner.remove_success_scratch()
    assert owner.owner_handle is None


def test_scratch_removal_tolerates_a_missing_cancellation_directory(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    del volume.owner_entry("owner-1").children["cancellation"]

    owner.remove_success_scratch()

    assert "owner-1" not in volume.parent.children


def test_scratch_removal_never_deletes_an_owner_root_that_was_replaced(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    volume.parent.children["owner-1"] = FakeEntry(directory=True)

    with pytest.raises(PermissionError, match="identity changed before deletion"):
        owner.remove_success_scratch()
    assert "owner-1" in volume.parent.children
    assert len(volume.handles) == 1


def test_scratch_removal_twice_is_harmless(volume: FakeWindowsVolume, tmp_path: Path) -> None:
    owner = _created_owner(tmp_path)
    owner.remove_success_scratch()

    owner.remove_success_scratch()

    assert owner.owner_handle is None


def test_the_lexical_identity_holds_for_an_untouched_owner_root(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)

    owner.verify_lexical_identity()

    assert len(volume.handles) == 2


def _replace_parent(volume: FakeWindowsVolume, tmp_path: Path) -> None:
    moved = FakeEntry(directory=True, children=dict(volume.parent.children))
    volume.roots[tmp_path] = moved


def _replace_named_owner(volume: FakeWindowsVolume, tmp_path: Path) -> None:
    volume.parent.children["owner-1"] = FakeEntry(directory=True)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (_replace_parent, "parent identity changed"),
        (_replace_named_owner, "^LSP owner root identity changed"),
    ],
)
def test_the_lexical_identity_refuses_a_replaced_path(
    volume: FakeWindowsVolume,
    tmp_path: Path,
    change: Callable[[FakeWindowsVolume, Path], None],
    message: str,
) -> None:
    owner = _created_owner(tmp_path)
    change(volume, tmp_path)

    with pytest.raises(RuntimeError, match=message):
        owner.verify_lexical_identity()
    assert len(volume.handles) == 2


def test_the_lexical_identity_refuses_a_held_root_that_moved(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    original_identity = volume.identity
    owner_handle = owner.owner_handle

    def held_root_moves(handle: int, *, directory: bool | None = None) -> tuple[int, bytes, bool]:
        found = original_identity(handle, directory=directory)
        if handle == owner_handle:
            return (found[0], b"moved", found[2])
        return found

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(windows_workspace, "identity", held_root_moves)
        with pytest.raises(RuntimeError, match="held LSP owner root identity changed"):
            owner.verify_lexical_identity()


def test_the_lexical_identity_needs_a_created_owner_root(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = OwnerDirectory.open(tmp_path / "owner-1")

    with pytest.raises(RuntimeError, match="was not created"):
        owner.verify_lexical_identity()


def test_closing_releases_both_handles_once(volume: FakeWindowsVolume, tmp_path: Path) -> None:
    owner = _created_owner(tmp_path)

    owner.close()
    owner.close()

    assert volume.handles == {}
    assert owner._closed
    assert owner.parent_handle == -1


def test_a_close_that_fails_keeps_the_handle_for_another_attempt(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    volume.fail("close_handle", OSError(6, "invalid handle"))

    with pytest.raises(OSError, match="invalid handle"):
        owner.close()
    assert not owner._closed
    assert owner.owner_handle is not None

    owner.close()

    assert owner._closed
    assert volume.handles == {}


def test_lexical_identity_handles_that_will_not_close_are_closed_by_the_next_check(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    volume.fail("close_handle", OSError(6, "invalid handle"))

    with pytest.raises(OSError, match="invalid handle"):
        owner.verify_lexical_identity()
    assert len(owner._pending_child_handles) == 1

    owner.verify_lexical_identity()

    assert owner._pending_child_handles == []
    assert len(volume.handles) == 2


def test_pending_child_handles_are_remembered_once_and_bounded(
    volume: FakeWindowsVolume, tmp_path: Path
) -> None:
    owner = _created_owner(tmp_path)
    bound = MAX_PENDING_CHILD_HANDLES
    for handle in range(bound):
        owner._remember_pending_child_handle(handle)
    owner._remember_pending_child_handle(0)

    assert owner._pending_child_handles == list(range(bound))
    with pytest.raises(RuntimeError, match="pending child handle bound was exhausted"):
        owner._remember_pending_child_handle(bound)
    owner._pending_child_handles.clear()
