"""The live lease and the failure evidence of an LSP child are published exactly."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest
import windows_workspace
from llm_wiki import (
    lsp_process_cleanup,
    lsp_process_generation,
    lsp_process_owner,
    lsp_process_state,
)
from lsp_process import (
    LspProcess,
    ProcessState,
)

from tests.lsp_process_helpers import (
    _no_lsp_lifecycle_owner_leaks,  # noqa: F401
    )
from tests.lsp_process_scenario_helpers import (
    _BARRIER_SECONDS,
    OWNER_NONCE,
    _cleanup_error_steps,
    _close_if_owned,
    _command,
    _mock_windows_artifact_owner,
    _owner_entry_names,
    _pid_alive,
    _remove_temporaries,
    _start,
)
from tests.slow_machine import SHORT_TIMEOUT


def _expect_blocked_lease_write(coordinator, owner) -> None:
    """Writing the lease keeps failing while its temporary cannot be removed."""
    lsp_process_state.acquire_lease(coordinator, time.monotonic() + 1)
    try:
        with pytest.raises(OSError, match="lease temporary deletion failed"):
            owner.write_lease({"state": "live"})
    finally:
        lsp_process_state.release_lease(coordinator)


def _is_blocked_temporary(name: str, prefix: str) -> bool:
    return name.startswith(prefix) and name.endswith(".tmp")


def _refuse_until_allowed(allowed, message: str) -> None:
    if not allowed.is_set():
        raise OSError(message)


def _mutate_failure_record(record: dict, mutation: str, pid: int) -> None:
    """One field of a terminal failure record that must break its identity."""
    mutations = {
        "wrong-owner": lambda: record.__setitem__("owner_nonce", "b" * 32),
        "wrong-generation": lambda: record.__setitem__("generation_nonce", "b" * 32),
        "wrong-code": lambda: record.__setitem__("code", "process_exited"),
        "missing-pid": lambda: record.pop("server_pid"),
        "wrong-pid": lambda: record.__setitem__("server_pid", pid + 1),
        "noncanonical-timestamp": lambda: record.__setitem__(
            "timestamp", "2026-07-24T12:34:56.1Z"
        ),
    }
    mutations[mutation]()


@pytest.mark.skipif(os.name != "posix", reason="POSIX atomic lease publication")
def test_posix_lease_publish_failure_preserves_previous_bytes_and_no_temp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / OWNER_NONCE
    owner = lsp_process_owner.OwnerDirectory.open(owner_root)
    owner.create(time.monotonic() + 1)
    owner.write_lease({"version": "old"})
    before = (owner_root / "lease.json").read_bytes()
    monkeypatch.setattr(
        os,
        "replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        owner.write_lease({"version": "new"})

    assert (owner_root / "lease.json").read_bytes() == before
    assert not list(owner_root.glob(".lease-*.tmp"))
    owner.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows lease sharing semantics")
def test_windows_lease_refresh_waits_for_a_temporary_reader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    lease_path = process.owner_root / "lease.json"
    before = lease_path.read_bytes()
    replace_file = windows_workspace.replace_file
    sharing_blocked = threading.Event()
    attempts = 0
    errors: list[BaseException] = []

    def observe_replace(handle: int, parent: int, name: str) -> None:
        nonlocal attempts
        attempts += 1
        try:
            replace_file(handle, parent, name)
        except OSError as error:
            code = getattr(error, "winerror", None) or error.errno
            if code in {5, 32, 33}:
                sharing_blocked.set()
            raise

    def refresh() -> None:
        try:
            lsp_process_generation.write_current_lease(process, time.monotonic() + 2)
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(
        windows_workspace,
        "replace_file",
        observe_replace,
    )
    reader = lease_path.open("rb")
    worker = threading.Thread(target=refresh)
    try:
        worker.start()
        assert sharing_blocked.wait(SHORT_TIMEOUT)
        assert worker.is_alive()
        reader.close()
        worker.join(SHORT_TIMEOUT)
        assert not worker.is_alive()
        assert errors == []
        assert attempts >= 2
        assert lease_path.read_bytes() != before
        assert not list(process.owner_root.glob(".lease-*.tmp"))
    finally:
        reader.close()
        worker.join(SHORT_TIMEOUT)
        if lsp_process_state.coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)


@pytest.mark.skipif(os.name != "nt", reason="Windows lease sharing semantics")
@pytest.mark.parametrize(
    ("error_code", "expected_attempts"),
    [(5, 2), (32, 2), (33, 2), (87, 1)],
)
def test_windows_lease_retries_only_transient_sharing_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error_code: int,
    expected_attempts: int,
) -> None:
    owner = _mock_windows_artifact_owner(
        monkeypatch,
        tmp_path,
        lambda _handle: None,
    )
    attempts = 0

    def replace_file(_handle: int, _parent: int, _name: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError(error_code, "injected lease replacement failure")

    monkeypatch.setattr(windows_workspace, "replace_file", replace_file)
    now = time.monotonic()
    if error_code == 87:
        with pytest.raises(OSError) as raised:
            owner.write_lease(
                {"state": "live"},
                deadline=now + 1,
                expires_monotonic=now + 30,
                retry_stop=threading.Event(),
            )
        assert raised.value.errno == error_code
    else:
        owner.write_lease(
            {"state": "live"},
            deadline=now + 1,
            expires_monotonic=now + 30,
            retry_stop=threading.Event(),
        )

    assert attempts == expected_attempts


@pytest.mark.skipif(os.name != "nt", reason="Windows lease sharing semantics")
def test_windows_lease_retry_cannot_outlive_the_published_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner = _mock_windows_artifact_owner(
        monkeypatch,
        tmp_path,
        lambda _handle: None,
    )
    now = time.monotonic()
    previous_expiry = now + 0.08
    owner.write_lease(
        {"generation": "old"},
        deadline=now + 1,
        expires_monotonic=previous_expiry,
        retry_stop=threading.Event(),
    )
    attempts = 0

    def sharing_violation(_handle: int, _parent: int, _name: str) -> None:
        nonlocal attempts
        attempts += 1
        raise OSError(32, "lease reader is still open")

    monkeypatch.setattr(
        windows_workspace,
        "replace_file",
        sharing_violation,
    )
    started = time.monotonic()

    with pytest.raises(OSError) as raised:
        owner.write_lease(
            {"generation": "new"},
            deadline=started + 1,
            expires_monotonic=started + 30,
            retry_stop=threading.Event(),
        )

    assert raised.value.errno == 32
    assert attempts >= 2
    assert time.monotonic() <= previous_expiry + 0.15


def test_successful_failure_evidence_is_published_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    write_failure = lsp_process_state.write_failure_record
    publications = 0

    def count_publication(*args: object, **kwargs: object) -> None:
        nonlocal publications
        publications += 1
        write_failure(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(lsp_process_state, "write_failure_record", count_publication)
    try:
        process._terminal_failure("injected_failure", time.monotonic() + 5)
    finally:
        monkeypatch.setattr(lsp_process_state, "write_failure_record", write_failure)
        if lsp_process_state.coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)

    assert publications == 1
    assert coordinator.cleanup_result.evidence == "success"


def test_failed_state_and_waiter_notification_follow_durable_failure_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    owner = coordinator.owner_directory
    heartbeat = coordinator.heartbeat_thread
    assert owner is not None and heartbeat is not None
    write_failure = lsp_process_state.write_failure_record
    write_lease = lsp_process_owner.OwnerDirectory.write_lease
    evidence_started = threading.Event()
    release_evidence = threading.Event()
    heartbeat_wrote = threading.Event()
    waiter_ready = threading.Event()
    waiter_finished = threading.Event()
    waiter_results: list[bool] = []
    cleanup_errors: list[BaseException] = []

    def block_evidence(*args: object, **kwargs: object) -> None:
        evidence_started.set()
        assert release_evidence.wait(SHORT_TIMEOUT)
        write_failure(*args, **kwargs)  # type: ignore[arg-type]

    def observe_heartbeat(current: object, record: object, **kwargs: object) -> None:
        if (
            current is owner
            and threading.current_thread() is heartbeat
            and evidence_started.is_set()
            and not release_evidence.is_set()
        ):
            heartbeat_wrote.set()
        write_lease(current, record, **kwargs)  # type: ignore[arg-type]

    def wait_for_failed() -> None:
        with coordinator.condition:
            waiter_ready.set()
            waiter_results.append(
                coordinator.condition.wait_for(
                    lambda: process.state is ProcessState.FAILED,
                    timeout=3,
                )
            )
        waiter_finished.set()

    def fail_terminally() -> None:
        try:
            process._terminal_failure("injected_failure", time.monotonic() + 5)
        except BaseException as error:
            cleanup_errors.append(error)

    monkeypatch.setattr(lsp_process_state, "write_failure_record", block_evidence)
    monkeypatch.setattr(lsp_process_owner.OwnerDirectory, "write_lease", observe_heartbeat)
    waiter = threading.Thread(target=wait_for_failed)
    cleanup = threading.Thread(target=fail_terminally)
    waiter.start()
    assert waiter_ready.wait(SHORT_TIMEOUT)
    cleanup.start()
    try:
        assert evidence_started.wait(SHORT_TIMEOUT), cleanup_errors
        assert coordinator.phase is lsp_process_state.LifecyclePhase.STOPPING_FAILURE
        assert process.state is ProcessState.DEGRADED
        assert waiter_finished.wait(0.2) is False
        assert not (process.owner_root / "failure.json").exists()
        assert (process.owner_root / "lease.json").is_file()
        coordinator.heartbeat_wake.set()
        assert heartbeat_wrote.wait(_BARRIER_SECONDS)
    finally:
        release_evidence.set()
        cleanup.join(SHORT_TIMEOUT)
        waiter.join(SHORT_TIMEOUT)
        monkeypatch.setattr(lsp_process_state, "write_failure_record", write_failure)
        if lsp_process_state.coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)

    assert not cleanup.is_alive()
    assert not waiter.is_alive()
    assert cleanup_errors == []
    assert waiter_results == [True]
    assert process.state is ProcessState.FAILED
    failure = json.loads((process.owner_root / "failure.json").read_bytes())
    lsp_process_state.validate_failure_record(
        failure,
        code="injected_failure",
        owner_nonce=process.owner_nonce,
        generation_nonce=process.generation_nonce,
        pid=process.process.pid,
    )


def test_failure_evidence_write_fault_is_observable_and_tree_cleanup_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--ignore-shutdown")
    coordinator = process._coordinator
    owner = coordinator.owner_directory
    assert owner is not None
    write_record = lsp_process_owner.OwnerDirectory.write_record
    fault_enabled = True

    def fail_failure(current: object, name: str, record: object) -> None:
        if current is owner and name == "failure.json" and fault_enabled:
            raise OSError("failure evidence flush failed")
        write_record(current, name, record)  # type: ignore[arg-type]

    monkeypatch.setattr(lsp_process_owner.OwnerDirectory, "write_record", fail_failure)
    with pytest.raises(OSError, match="failure evidence flush failed"):
        process._terminal_failure("injected_failure", time.monotonic() + 2)

    failure = process.owner_root / "failure.json"
    if failure.exists():
        json.loads(failure.read_bytes())
    generation = coordinator.active
    assert generation is None or generation.tree is None or generation.process.poll() is not None
    assert coordinator.cleanup_result.evidence == "failed"
    assert coordinator.cleanup_result.ownership_pending is True
    assert process.state is ProcessState.DEGRADED
    assert (process.owner_root / "lease.json").is_file()

    fault_enabled = False
    process.close(time.monotonic() + 5)
    assert process.state is ProcessState.FAILED
    assert json.loads(failure.read_bytes())["code"] == "injected_failure"
    assert not (process.owner_root / "lease.json").exists()


def test_evidence_payload_failure_leaves_no_partial_canonical_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / OWNER_NONCE
    owner = lsp_process_owner.OwnerDirectory.open(owner_root)
    owner.create(time.monotonic() + 1)

    if os.name == "nt":
        real_windows_write = windows_workspace.write_all

        def partial_windows_write(handle: int, payload: bytes, *, chunk_bytes: int) -> None:
            real_windows_write(handle, payload[:5], chunk_bytes=chunk_bytes)
            raise OSError("evidence write failed")

        monkeypatch.setattr(
            windows_workspace,
            "write_all",
            partial_windows_write,
        )
    else:
        real_write = lsp_process_owner.write_all_descriptor

        def partial_write(descriptor: int, payload: bytes) -> None:
            os.write(descriptor, payload[:5])
            raise OSError("evidence write failed")

        monkeypatch.setattr(lsp_process_owner, "write_all_descriptor", partial_write)

    with pytest.raises(OSError, match="evidence (flush|write) failed"):
        owner.write_record("failure.json", {"code": "injected"})

    canonical = owner_root / "failure.json"
    assert not canonical.exists() or json.loads(canonical.read_bytes()) == {
        "code": "injected"
    }
    assert not list(owner_root.glob(".evidence-*.tmp"))
    if os.name != "nt":
        monkeypatch.setattr(lsp_process_owner, "write_all_descriptor", real_write)
    owner.close()


def test_existing_canonical_but_invalid_failure_evidence_is_never_accepted(
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--ignore-shutdown")
    failure = process.owner_root / "failure.json"
    failure.write_bytes(
        json.dumps(
            {
                "code": None,
                "generation_nonce": "invalid",
                "owner_nonce": process.owner_nonce,
                "timestamp": "not-a-timestamp",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    try:
        with pytest.raises(ValueError, match="failure evidence"):
            process._terminal_failure("injected_failure", time.monotonic() + 5)

        assert process.process.poll() is not None
        assert process._coordinator.cleanup_result.evidence == "failed"
        assert process._coordinator.phase is lsp_process_state.LifecyclePhase.CLEANUP_PENDING
        assert (process.owner_root / "lease.json").is_file()
    finally:
        if failure.exists():
            failure.unlink()
        if lsp_process_state.coordinator_has_ownership(process._coordinator):
            process.close(time.monotonic() + 5)


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong-owner",
        "wrong-generation",
        "wrong-code",
        "missing-pid",
        "wrong-pid",
        "noncanonical-timestamp",
    ],
)
def test_existing_failure_evidence_must_match_terminal_identity_and_stays_sticky(
    tmp_path: Path,
    mutation: str,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--ignore-shutdown")
    coordinator = process._coordinator
    record: dict[str, object] = {
        "code": "heartbeat_failed",
        "generation_nonce": process.generation_nonce,
        "owner_nonce": process.owner_nonce,
        "server_pid": process.process.pid,
        "timestamp": "2026-07-24T12:34:56.123456Z",
    }
    _mutate_failure_record(record, mutation, process.process.pid)
    failure = process.owner_root / "failure.json"
    failure.write_bytes(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )

    try:
        lsp_process_state.queue_owner_failure(coordinator, "injected owner failure")
        recovery = coordinator.recovery_thread
        assert recovery is not None
        recovery.join(SHORT_TIMEOUT)

        assert not recovery.is_alive()
        assert coordinator.failure_evidence_identity is not None
        assert coordinator.failure_evidence_identity.code == "heartbeat_failed"
        assert coordinator.background_cleanup_error is not None
        assert coordinator.background_cleanup_error.error_type == "ValueError"
        assert coordinator.cleanup_result.evidence == "failed"
        assert coordinator.cleanup_result.ownership_pending is True
        assert coordinator.owner_directory is not None
        assert (process.owner_root / "lease.json").is_file()

        failure.unlink()
        with pytest.raises(RuntimeError, match=r"background cleanup failed \(ValueError\)"):
            process.close(time.monotonic() + 5)
        assert not lsp_process_state.coordinator_has_ownership(coordinator)
        process.close(time.monotonic() + 1)
    finally:
        _remove_failure_and_release_ownership(process, coordinator, failure)


def _remove_failure_and_release_ownership(process, coordinator, failure: Path) -> None:
    if failure.exists():
        failure.unlink()
    if not lsp_process_state.coordinator_has_ownership(coordinator):
        return
    try:
        process.close(time.monotonic() + 5)
    except ValueError:
        process.close(time.monotonic() + 5)


@pytest.mark.parametrize("artifact", ["evidence", "lease"])
def test_posix_partial_artifact_write_removes_temporary_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    artifact: str,
) -> None:
    class FileInfo:
        st_dev = 1
        st_ino = 2
        st_mode = stat.S_IFREG | 0o600

    class PosixOperations:
        name = "posix"
        O_WRONLY = os.O_WRONLY
        O_CREAT = os.O_CREAT
        O_EXCL = os.O_EXCL
        O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
        O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)

        def __init__(self) -> None:
            self.created: list[str] = []
            self.closed: list[int] = []
            self.unlinked: list[str] = []

        def open(
            self,
            path: str,
            _flags: int,
            _mode: int,
            *,
            dir_fd: int,
        ) -> int:
            assert dir_fd == 202
            self.created.append(path)
            return 303

        @staticmethod
        def fstat(descriptor: int) -> FileInfo:
            assert descriptor == 303
            return FileInfo()

        def close(self, descriptor: int) -> None:
            self.closed.append(descriptor)

        def unlink(self, path: str, *, dir_fd: int) -> None:
            assert dir_fd == 202
            self.unlinked.append(path)

    operations = PosixOperations()
    owner = lsp_process_owner.OwnerDirectory(
        tmp_path / OWNER_NONCE,
        parent_handle=101,
        parent_identity=object(),
        owner_handle=202,
        owner_identity=object(),
        owner_permissions_verified=True,
    )
    monkeypatch.setattr(lsp_process_owner, "os", operations)
    monkeypatch.setattr(
        lsp_process_owner,
        "write_all_descriptor",
        lambda _descriptor, _payload: (_ for _ in ()).throw(
            OSError("partial artifact write")
        ),
    )

    with pytest.raises(OSError, match="partial artifact write"):
        if artifact == "evidence":
            owner.write_record("failure.json", {"code": "injected"})
        else:
            owner.write_lease({"state": "live"})

    assert len(operations.created) == 1
    assert operations.closed == [303]
    assert operations.unlinked == operations.created


def _block_native_temporary_deletion(
    monkeypatch: pytest.MonkeyPatch,
    owner_root: Path,
    prefix: str,
    message: str,
) -> tuple[threading.Event, list[str]]:
    allowed = threading.Event()
    attempts: list[str] = []
    if os.name == "nt":
        workspace = windows_workspace
        real_create = workspace.create_file
        real_open = workspace.open_deletable_file
        real_delete = workspace.delete_handle
        temporary_handles: set[int] = set()

        def create_file(parent: int, name: str) -> int:
            handle = real_create(parent, name)
            if name.startswith(prefix) and name.endswith(".tmp"):
                temporary_handles.add(handle)
            return handle

        def open_deletable_file(parent: int, name: str) -> int:
            handle = real_open(parent, name)
            if name.startswith(prefix) and name.endswith(".tmp"):
                temporary_handles.add(handle)
            return handle

        def delete_handle(handle: int) -> None:
            if handle in temporary_handles:
                attempts.extend(
                    sorted(path.name for path in owner_root.glob(f"{prefix}*.tmp"))
                )
                _refuse_until_allowed(allowed, message)
            real_delete(handle)
            temporary_handles.discard(handle)

        monkeypatch.setattr(workspace, "create_file", create_file)
        monkeypatch.setattr(workspace, "open_deletable_file", open_deletable_file)
        monkeypatch.setattr(workspace, "delete_handle", delete_handle)
    else:
        real_unlink = os.unlink

        def unlink(name: str, *, dir_fd: int | None = None) -> None:
            if _is_blocked_temporary(name, prefix):
                attempts.append(name)
                _refuse_until_allowed(allowed, message)
            if dir_fd is None:
                real_unlink(name)
                return
            real_unlink(name, dir_fd=dir_fd)

        monkeypatch.setattr(os, "unlink", unlink)
    return allowed, attempts


def test_failure_temp_cleanup_blocks_canonical_evidence_finalization_until_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--ignore-shutdown")
    coordinator = process._coordinator
    owner = coordinator.owner_directory
    assert owner is not None
    deletion_allowed, deletion_attempts = _block_native_temporary_deletion(
        monkeypatch,
        process.owner_root,
        ".evidence-",
        "evidence temporary deletion failed",
    )

    try:
        if os.name == "nt":
            writer_owner = windows_workspace
            writer_name = "write_all"
        else:
            writer_owner = lsp_process_owner
            writer_name = "write_all_descriptor"
        real_write = getattr(writer_owner, writer_name)
        monkeypatch.setattr(
            writer_owner,
            writer_name,
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("evidence temporary write failed")
            ),
        )
        with pytest.raises(OSError, match="evidence temporary deletion failed"):
            owner.write_record("failure.json", {"code": "one-shot"})
        assert len(owner._pending_temp_names) == 1
        assert len(list(process.owner_root.glob(".evidence-*.tmp"))) == 1

        deletion_allowed.set()
        owner._retry_pending_temp_names()
        monkeypatch.setattr(writer_owner, writer_name, real_write)
        assert owner._pending_temp_names == []
        assert not list(process.owner_root.glob(".evidence-*.tmp"))

        lsp_process_state.write_failure_record(
            owner,
            code="injected_failure",
            owner_nonce=process.owner_nonce,
            generation_nonce=process.generation_nonce,
            pid=process.process.pid,
        )
        deletion_allowed.clear()
        with pytest.raises(OSError, match="evidence temporary deletion failed"):
            process._terminal_failure("injected_failure", time.monotonic() + 5)

        assert coordinator.phase is lsp_process_state.LifecyclePhase.CLEANUP_PENDING
        assert coordinator.cleanup_result.evidence == "failed"
        assert coordinator.cleanup_result.ownership_pending is True
        assert coordinator.owner_directory is owner
        assert owner._closed is False
        assert process.state is ProcessState.DEGRADED
        assert (process.owner_root / "lease.json").is_file()
        pending = list(owner._pending_temp_names)
        assert len(pending) == 1
        assert _owner_entry_names(process) == {
            "cancellation",
            "owner.json",
            "lease.json",
            "failure.json",
            pending[0],
        }
        first_attempt_count = len(deletion_attempts)

        with pytest.raises(OSError, match="evidence temporary deletion failed"):
            process.close(time.monotonic() + 5)

        assert len(deletion_attempts) > first_attempt_count
        assert owner._pending_temp_names == pending
        assert list(process.owner_root.glob(".evidence-*.tmp")) == [
            process.owner_root / pending[0]
        ]
        assert ("evidence", "OSError") in _cleanup_error_steps(coordinator)

        deletion_allowed.set()
        process.close(time.monotonic() + 5)

        assert owner._pending_temp_names == []
        assert owner._closed is True
        assert coordinator.owner_directory is None
        assert coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_FAILURE
        assert process.state is ProcessState.FAILED
        assert _owner_entry_names(process) == {
            "cancellation",
            "owner.json",
            "failure.json",
        }
    finally:
        deletion_allowed.set()
        _remove_temporaries(process.owner_root, ".evidence-*.tmp")
        _close_if_owned(process, coordinator)


def test_lease_temp_cleanup_blocks_success_finalization_until_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    owner = coordinator.owner_directory
    assert owner is not None
    lease = process.owner_root / "lease.json"
    lease_before = lease.read_bytes()
    deletion_allowed, deletion_attempts = _block_native_temporary_deletion(
        monkeypatch,
        process.owner_root,
        ".lease-",
        "lease temporary deletion failed",
    )
    if os.name == "nt":
        writer_owner = windows_workspace
        writer_name = "write_all"
    else:
        writer_owner = lsp_process_owner
        writer_name = "write_all_descriptor"
    real_write = getattr(writer_owner, writer_name)
    monkeypatch.setattr(
        writer_owner,
        writer_name,
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("lease temporary write failed")
        ),
    )

    try:
        for _attempt in range(2):
            _expect_blocked_lease_write(coordinator, owner)
        monkeypatch.setattr(writer_owner, writer_name, real_write)

        pending = list(owner._pending_temp_names)
        assert len(pending) == 1
        assert len(set(deletion_attempts)) == 1
        assert lease.read_bytes() == lease_before
        assert _owner_entry_names(process) == {
            "cancellation",
            "owner.json",
            "lease.json",
            pending[0],
        }

        with pytest.raises(OSError, match="lease temporary deletion failed"):
            process.shutdown(time.monotonic() + 5)

        assert coordinator.terminal_outcome == "success"
        assert coordinator.phase is lsp_process_state.LifecyclePhase.CLEANUP_PENDING
        assert coordinator.cleanup_result.lease_removal == "failed"
        assert coordinator.cleanup_result.ownership_pending is True
        assert coordinator.owner_directory is owner
        assert owner._closed is False
        assert lease.read_bytes() == lease_before
        assert owner._pending_temp_names == pending
        assert _owner_entry_names(process) == {
            "cancellation",
            "owner.json",
            "lease.json",
            pending[0],
        }

        with pytest.raises(OSError, match="lease temporary deletion failed"):
            process.shutdown(time.monotonic() + 5)
        assert owner._pending_temp_names == pending
        assert list(process.owner_root.glob(".lease-*.tmp")) == [
            process.owner_root / pending[0]
        ]
        assert ("lease_removal", "OSError") in _cleanup_error_steps(coordinator)

        deletion_allowed.set()
        process.shutdown(time.monotonic() + 5)

        assert owner._pending_temp_names == []
        assert owner._closed is True
        assert coordinator.owner_directory is None
        assert coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_SUCCESS
        assert not process.owner_root.exists()
    finally:
        monkeypatch.setattr(writer_owner, writer_name, real_write)
        deletion_allowed.set()
        _remove_temporaries(process.owner_root, ".lease-*.tmp")
        _close_if_owned(process, coordinator)


def test_startup_rollback_kills_descendant_after_direct_leader_exits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "startup-descendant.pid"
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    def fail_after_leader_exit(*_args: object, **_kwargs: object) -> None:
        assert children
        children[0].wait(timeout=SHORT_TIMEOUT)
        raise RuntimeError("startup publication failed")

    monkeypatch.setattr(lsp_process_state.subprocess, "Popen", popen)
    monkeypatch.setattr(lsp_process_state, "write_owner_record", fail_after_leader_exit)
    owner = tmp_path / OWNER_NONCE

    with pytest.raises((RuntimeError, lsp_process_cleanup.StartupCleanupError)) as raised:
        LspProcess.start(
            _command(
                "--spawn-descendant",
                "--descendant-pid-file",
                str(pid_file),
                "--exit-after-descendant-spawn",
            ),
            cwd=tmp_path,
            owner_root=owner,
        )

    descendant = int(pid_file.read_text(encoding="ascii"))
    settle_deadline = time.monotonic() + 2
    while _pid_alive(descendant) and time.monotonic() < settle_deadline:
        time.sleep(0.02)
    if _pid_alive(descendant):
        assert isinstance(raised.value, lsp_process_cleanup.StartupCleanupError)
        assert raised.value.coordinator.cleanup_result.ownership_pending is True
    else:
        assert json.loads((owner / "failure.json").read_bytes())["code"] == "startup_failed"
    if isinstance(raised.value, lsp_process_cleanup.StartupCleanupError):
        raised.value.retry_cleanup(time.monotonic() + 5)
