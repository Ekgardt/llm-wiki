"""The managed Pyright installer owns one lock, cleans only its own scratch, and publishes a complete tree or none."""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import threading
import time
from pathlib import Path

import install_pyright as installer_module
import pytest
from install_pyright import PyrightInstallError, install_pyright
from reliable_memory import canonical_json_bytes, sha256_bytes

from tests.install_pyright_helpers import _artifact, _Response, _root
from tests.pyright_installer_helpers import (
    _assert_no_owned_scratch,
    _error_code,
)


def _lock_metadata(*, pid: int, process_start: str, nonce: str = "a" * 32) -> bytes:
    return canonical_json_bytes(
        {
            "acquired_at_unix_ns": time.time_ns(),
            "nonce": nonce,
            "pid": pid,
            "process_start": process_start,
        }
    )


def _windows_process_handle_count() -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL
    count = wintypes.DWORD()
    if not kernel32.GetProcessHandleCount(
        kernel32.GetCurrentProcess(), ctypes.byref(count)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(count.value)


def test_download_interruption_cleans_only_owned_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    parent = state_root / "cache/code-tools/pyright"
    parent.mkdir(parents=True)
    unrelated = parent / "keep-me"
    unrelated.write_bytes(b"unrelated")

    class InterruptedResponse(_Response):
        def __init__(self) -> None:
            super().__init__(b"x" * 1024)
            self.calls = 0

        def read1(self, size: int = -1) -> bytes:
            self.calls += 1
            if self.calls == 1:
                return b"x" * 64
            raise OSError("download interrupted")

    monkeypatch.setattr(
        installer_module,
        "_open_pinned_url",
        lambda *args, **kwargs: InterruptedResponse(),
    )

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root)

    assert _error_code(error) == "pyright_download_failed"
    assert unrelated.read_bytes() == b"unrelated"
    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


def test_lock_write_failure_is_typed_and_cleans_owned_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)

    def fail_lock_write(*args: object, **kwargs: object) -> None:
        raise OSError("lock write interrupted")

    monkeypatch.setattr(installer_module, "write_handle", fail_lock_write)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == "pyright_install_io_failed"
    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


def test_lock_that_cannot_be_retained_is_left_to_its_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing the lock we just created is contention, not a broken install.

    A hosted runner produced `new Pyright install lock could not be retained`,
    and the old code answered by deleting the lock file — one somebody else was
    holding — and failing the installation. Backing off leaves the file alone
    and lets the caller wait for it.
    """
    parent_path = tmp_path / "pyright"
    parent_path.mkdir()
    parent = installer_module.open_absolute_directory(parent_path, writable=True)
    monkeypatch.setattr(installer_module, "lock_handle_nonblocking", lambda _handle: False)

    try:
        owned = installer_module.create_owned_lock(parent, time.monotonic() + 5)
    finally:
        parent.close()

    assert owned is None
    assert (parent_path / installer_module.LOCK_NAME).is_file()


def test_lock_metadata_is_canonical_and_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_path = tmp_path / "pyright"
    parent_path.mkdir()
    parent = installer_module.open_absolute_directory(parent_path, writable=True)
    real_fsync_file = installer_module.fsync_file
    real_fsync_directory = installer_module.fsync_directory
    synced: list[str] = []

    def fsync_file(handle: object) -> None:
        real_fsync_file(handle)
        synced.append("file")

    def fsync_directory(handle: object) -> None:
        real_fsync_directory(handle)
        synced.append("directory")

    monkeypatch.setattr(installer_module, "fsync_file", fsync_file)
    monkeypatch.setattr(installer_module, "fsync_directory", fsync_directory)
    lock = None
    try:
        lock = installer_module.try_create_lock(parent, time.monotonic() + 5)
        assert lock is not None
        installer_module.seek_start(lock.handle)
        raw = installer_module.read_handle(
            lock.handle, installer_module.file_size(lock.handle)
        )
        metadata = json.loads(raw)

        assert raw == canonical_json_bytes(metadata)
        assert set(metadata) == {
            "acquired_at_unix_ns",
            "nonce",
            "pid",
            "process_start",
        }
        assert isinstance(metadata["acquired_at_unix_ns"], int)
        assert metadata["pid"] == os.getpid()
        assert isinstance(metadata["process_start"], str)
        assert metadata["process_start"]
        assert len(metadata["nonce"]) == 32
        assert all(character in "0123456789abcdef" for character in metadata["nonce"])
        assert synced == ["file", "directory"]
    finally:
        if lock is not None:
            lock.cleanup()
        parent.close()


@pytest.mark.parametrize(
    "observed_process_start",
    [None, "reused-process"],
    ids=("dead", "pid-reused"),
)
def test_dead_or_pid_reused_install_lock_is_reclaimed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observed_process_start: str | None,
) -> None:
    parent_path = tmp_path / "pyright"
    parent_path.mkdir()
    lock_path = parent_path / ".install-pyright-lock"
    recorded = installer_module.process_start_identity(os.getpid())
    observed = None
    if observed_process_start is not None:
        observed = recorded.rsplit(":", 1)[0] + ":1234567890123456789"
    lock_path.write_bytes(
        _lock_metadata(pid=424242, process_start=recorded)
    )
    monkeypatch.setattr(
        installer_module,
        "process_start_identity",
        lambda pid: observed if pid == 424242 else recorded,
        raising=False,
    )
    parent = installer_module.open_absolute_directory(parent_path, writable=True)
    lock = None
    try:
        lock = installer_module.try_create_lock(parent, time.monotonic() + 5)

        assert lock is not None
        assert lock.identity == installer_module.path_identity(lock.handle)
    finally:
        if lock is not None:
            lock.cleanup()
        parent.close()


def test_incomplete_lock_gets_initialization_grace_then_is_reclaimed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_path = tmp_path / "pyright"
    parent_path.mkdir()
    lock_path = parent_path / ".install-pyright-lock"
    lock_path.write_bytes(b"")
    parent = installer_module.open_absolute_directory(parent_path, writable=True)
    lock = None
    try:
        assert installer_module.try_create_lock(parent, time.monotonic() + 5) is None
        old = time.time() - 60
        os.utime(lock_path, (old, old))
        monkeypatch.setattr(
            installer_module,
            "LOCK_INITIALIZATION_GRACE_SECONDS",
            10.0,
            raising=False,
        )

        lock = installer_module.try_create_lock(parent, time.monotonic() + 5)

        assert lock is not None
    finally:
        if lock is not None:
            lock.cleanup()
        parent.close()


def _release_attempts(attempts, winners) -> None:
    for lock in winners:
        lock.cleanup()
    for parent, _lock in attempts:
        parent.close()


def test_concurrent_stale_lock_reclaimers_have_one_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_path = tmp_path / "pyright"
    parent_path.mkdir()
    recorded = installer_module.process_start_identity(os.getpid())
    (parent_path / ".install-pyright-lock").write_bytes(
        _lock_metadata(pid=424242, process_start=recorded)
    )
    monkeypatch.setattr(
        installer_module,
        "process_start_identity",
        lambda pid: None if pid == 424242 else recorded,
        raising=False,
    )

    start = threading.Barrier(2)
    acquired = threading.Event()

    def race_for_lock(parent, deadline):
        # Retry the way the installer does: losing one attempt means another
        # reclaimer got there first, not that this one may never proceed. Both
        # threads try at least once, and the loser stops as soon as the winner
        # holds the lock rather than spinning out a deadline sized for the
        # slowest supported machine.
        lock = None
        while lock is None:
            lock = installer_module.try_create_lock(parent, deadline)
            if acquired.is_set() or time.monotonic() >= deadline:
                break
        return lock

    def try_acquire() -> tuple[object, object | None]:
        parent = installer_module.open_absolute_directory(parent_path, writable=True)
        deadline = time.monotonic() + 120
        start.wait(timeout=120)
        lock = race_for_lock(parent, deadline)
        if lock is not None:
            acquired.set()
        return parent, lock

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        attempts = list(executor.map(lambda _index: try_acquire(), range(2)))

    winners = [lock for _parent, lock in attempts if lock is not None]
    try:
        assert len(winners) == 1
    finally:
        _release_attempts(attempts, winners)


def test_lock_release_refuses_same_file_with_changed_nonce(
    tmp_path: Path,
) -> None:
    parent_path = tmp_path / "pyright"
    parent_path.mkdir()
    lock_path = parent_path / ".install-pyright-lock"
    parent = installer_module.open_absolute_directory(parent_path, writable=True)
    lock = installer_module.try_create_lock(parent, time.monotonic() + 5)
    assert lock is not None
    try:
        lock.close()
        replacement = _lock_metadata(
            pid=os.getpid(), process_start="replacement", nonce="b" * 32
        )
        lock_path.write_bytes(replacement)

        lock.cleanup()

        assert lock_path.read_bytes() == replacement
    finally:
        lock_path.unlink(missing_ok=True)
        parent.close()


@pytest.mark.parametrize(
    ("target", "expected_message", "expected_code"),
    [
        (
            "_copy_member_data",
            "extract interrupted",
            "pyright_archive_extract_failed",
        ),
        ("fsync_file", "fsync interrupted", "pyright_install_fsync_failed"),
        (
            "atomic_publish_noreplace",
            "rename interrupted",
            "pyright_publish_failed",
        ),
    ],
    ids=("extract", "fsync", "rename"),
)
def test_extract_fsync_and_rename_interruptions_cleanup_owned_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    expected_message: str,
    expected_code: str,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    parent = state_root / "cache/code-tools/pyright"
    parent.mkdir(parents=True)
    unrelated = parent / "keep-me"
    unrelated.write_bytes(b"unrelated")

    def interrupt(*args: object, **kwargs: object) -> None:
        raise OSError(expected_message)

    if target == "fsync_file":
        real_fsync = installer_module.fsync_file
        calls = 0

        def interrupt_stage_fsync(*args: object, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError(expected_message)
            real_fsync(*args, **kwargs)

        monkeypatch.setattr(installer_module, target, interrupt_stage_fsync)
    else:
        monkeypatch.setattr(installer_module, target, interrupt)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == expected_code
    assert unrelated.read_bytes() == b"unrelated"
    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


def test_keyboard_interrupt_after_real_publish_preserves_complete_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    real_publish = installer_module.atomic_publish_noreplace
    moved_states: list[bool] = []

    def publish_then_interrupt(
        stage: installer_module.Stage,
        parent: installer_module.Handle,
        name: str,
    ) -> None:
        real_publish(stage, parent, name)
        moved_states.append(stage.published)
        stage.published = False
        raise KeyboardInterrupt("interrupted after atomic publish")

    monkeypatch.setattr(
        installer_module,
        "atomic_publish_noreplace",
        publish_then_interrupt,
    )

    with pytest.raises(KeyboardInterrupt, match="after atomic publish"):
        install_pyright(state_root=state_root, artifact=artifact.path)

    root = _root(state_root)
    assert (root / "install-manifest.json").is_file()
    assert (root / "package/package.json").is_file()
    assert (root / "package/langserver.index.js").read_bytes() == artifact.server_bytes
    assert moved_states == [True]
    _assert_no_owned_scratch(state_root)

    monkeypatch.setattr(installer_module, "atomic_publish_noreplace", real_publish)
    artifact.path.unlink()
    result = install_pyright(
        state_root=state_root,
        artifact=tmp_path / "must-not-be-read.tgz",
    )

    assert result.root == root
    assert result.server_sha256 == sha256_bytes(artifact.server_bytes)
    _assert_no_owned_scratch(state_root)


def test_keyboard_interrupt_before_publish_cleans_owned_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)

    def interrupt_before_publish(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt("interrupted before atomic publish")

    monkeypatch.setattr(
        installer_module,
        "atomic_publish_noreplace",
        interrupt_before_publish,
    )

    with pytest.raises(KeyboardInterrupt, match="before atomic publish"):
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


def test_stage_cleanup_leaves_moved_root_and_replacement_staging_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_path = tmp_path / "pyright"
    parent_path.mkdir()
    parent = installer_module.open_absolute_directory(parent_path, writable=True)
    stage = installer_module.new_stage(parent)
    deadline = time.monotonic() + 5
    final_name = "published-stage"
    final_path = parent_path / final_name
    replacement_path = parent_path / stage.name
    try:
        stage.write_bytes(("owned.txt",), b"owned", deadline)
        installer_module.atomic_publish_noreplace(stage, parent, final_name)
        stage.published = False
        replacement_path.mkdir()
        (replacement_path / "replacement.txt").write_bytes(b"replacement")
        monkeypatch.setattr(
            installer_module,
            "clear_directory",
            lambda _root: pytest.fail("cleanup cleared a root without staging ownership"),
        )

        stage.cleanup()

        assert (final_path / "owned.txt").read_bytes() == b"owned"
        assert (replacement_path / "replacement.txt").read_bytes() == b"replacement"
    finally:
        if not stage.root.closed:
            stage.root.close()
        parent.close()


def test_stage_directory_fsync_failure_cleans_unpublished_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    real_sync = installer_module.Stage.sync_directories
    real_fsync = installer_module.fsync_directory
    syncing_stage = False

    def sync_directories(self: object, deadline: float) -> None:
        nonlocal syncing_stage
        syncing_stage = True
        try:
            real_sync(self, deadline)
        finally:
            syncing_stage = False

    def fail_stage_directory(handle: object) -> None:
        if syncing_stage:
            raise OSError("stage directory fsync interrupted")
        real_fsync(handle)

    monkeypatch.setattr(installer_module.Stage, "sync_directories", sync_directories)
    monkeypatch.setattr(installer_module, "fsync_directory", fail_stage_directory)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == "pyright_install_fsync_failed"
    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


@pytest.mark.skipif(os.name != "nt", reason="Windows flush contract")
@pytest.mark.parametrize("failed_creation", [1, 2, 3], ids=("cache", "code-tools", "pyright"))
def test_windows_runtime_parent_creation_requires_successful_directory_flush(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_creation: int,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    calls = 0

    def fail_selected_creation(handle: int) -> bool:
        nonlocal calls
        calls += 1
        return calls != failed_creation

    monkeypatch.setattr(
        installer_module._windows_workspace,
        "flush_directory",
        fail_selected_creation,
    )

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == "pyright_install_fsync_failed"
    assert calls == failed_creation
    assert not _root(state_root).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle ownership contract")
def test_windows_parent_flush_failures_release_handles_for_repeated_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _windows_process_handle_count()

    for failed_creation in (1, 2, 3):
        for attempt in range(24):
            state_root = tmp_path / f"level-{failed_creation}-{attempt}" / "state"
            _assert_parent_flush_failure_cleans_up(monkeypatch, state_root, failed_creation)

    assert _windows_process_handle_count() <= baseline + 2


def _assert_parent_flush_failure_cleans_up(
    monkeypatch: pytest.MonkeyPatch, state_root: Path, failed_creation: int
) -> None:
    calls = 0

    def fail_selected_creation(handle: int) -> bool:
        nonlocal calls
        calls += 1
        return calls != failed_creation

    with monkeypatch.context() as scoped:
        scoped.setattr(
            installer_module._windows_workspace,
            "flush_directory",
            fail_selected_creation,
        )
        with pytest.raises(PyrightInstallError) as error:
            install_pyright(state_root=state_root)

    assert _error_code(error) == "pyright_install_fsync_failed"
    assert calls == failed_creation
    shutil.rmtree(state_root / "cache")
    (state_root / "run").rmdir()
    state_root.rmdir()
    state_root.parent.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle ownership contract")
def test_windows_parent_flush_primary_error_retains_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    real_close = installer_module._windows_workspace.close_handle
    close_calls: dict[int, int] = {}
    flush_failed = False
    faulted = False

    def close_then_fail_once(handle: int) -> None:
        nonlocal faulted
        if flush_failed:
            close_calls[handle] = close_calls.get(handle, 0) + 1
        real_close(handle)
        if flush_failed and not faulted:
            faulted = True
            raise OSError("injected close failure")

    def fail_flush(handle: int) -> bool:
        nonlocal flush_failed
        flush_failed = True
        return False

    monkeypatch.setattr(
        installer_module._windows_workspace,
        "flush_directory",
        fail_flush,
    )
    monkeypatch.setattr(
        installer_module._windows_workspace,
        "close_handle",
        close_then_fail_once,
    )

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root)

    assert _error_code(error) == "pyright_install_fsync_failed"
    assert isinstance(error.value.__cause__, OSError)
    assert str(error.value.__cause__) == "injected close failure"
    assert isinstance(error.value.__cause__.__cause__, OSError)
    assert str(error.value.__cause__.__cause__) == "directory flush failed"
    assert faulted is True
    assert len(close_calls) == 2
    assert set(close_calls.values()) == {1}
    shutil.rmtree(state_root / "cache")

    state_root = tmp_path / "close-transition-state"
    close_calls.clear()
    flush_failed = False
    faulted = False

    def successful_flush(handle: int) -> bool:
        nonlocal flush_failed
        flush_failed = True
        return True

    monkeypatch.setattr(
        installer_module._windows_workspace,
        "flush_directory",
        successful_flush,
    )

    with pytest.raises(PyrightInstallError) as transition_error:
        install_pyright(state_root=state_root)

    assert _error_code(transition_error) == "pyright_state_root_unsafe"
    assert isinstance(transition_error.value.__cause__, OSError)
    assert str(transition_error.value.__cause__) == "injected close failure"
    assert faulted is True
    assert len(close_calls) == 2
    assert set(close_calls.values()) == {1}
    shutil.rmtree(state_root / "cache")


@pytest.mark.skipif(os.name != "nt", reason="Windows flush contract")
def test_windows_stage_flush_false_fails_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    real_sync = installer_module.Stage.sync_directories
    real_flush = installer_module._windows_workspace.flush_directory
    syncing_stage = False

    def sync_directories(self: object, deadline: float) -> None:
        nonlocal syncing_stage
        syncing_stage = True
        try:
            real_sync(self, deadline)
        finally:
            syncing_stage = False

    def fail_stage_flush(handle: int) -> bool:
        return False if syncing_stage else real_flush(handle)

    monkeypatch.setattr(installer_module.Stage, "sync_directories", sync_directories)
    monkeypatch.setattr(
        installer_module._windows_workspace,
        "flush_directory",
        fail_stage_flush,
    )

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == "pyright_install_fsync_failed"
    assert not _root(state_root).exists()
    _assert_no_owned_scratch(state_root)


@pytest.mark.skipif(os.name != "nt", reason="Windows flush contract")
def test_windows_publication_parent_flush_false_keeps_complete_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    real_publish = installer_module.atomic_publish_noreplace
    published = False

    def publish(*args: object, **kwargs: object) -> None:
        nonlocal published
        real_publish(*args, **kwargs)
        published = True

    def fail_published_parent(handle: int) -> bool:
        return not published

    monkeypatch.setattr(installer_module, "atomic_publish_noreplace", publish)
    monkeypatch.setattr(
        installer_module._windows_workspace,
        "flush_directory",
        fail_published_parent,
    )

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == "pyright_install_fsync_failed"
    assert published is True
    assert (_root(state_root) / "install-manifest.json").is_file()
    _assert_no_owned_scratch(state_root)


def test_parent_fsync_failure_after_publish_keeps_complete_final_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    real_publish = installer_module.atomic_publish_noreplace
    real_fsync = installer_module.fsync_directory
    published = False

    def publish(*args: object, **kwargs: object) -> None:
        nonlocal published
        real_publish(*args, **kwargs)
        published = True

    def fail_parent_after_publish(handle: object) -> None:
        if published:
            raise OSError("parent fsync interrupted")
        real_fsync(handle)

    monkeypatch.setattr(installer_module, "atomic_publish_noreplace", publish)
    monkeypatch.setattr(installer_module, "fsync_directory", fail_parent_after_publish)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == "pyright_install_fsync_failed"
    assert (_root(state_root) / "install-manifest.json").is_file()
    artifact.path.unlink()
    assert install_pyright(
        state_root=state_root,
        artifact=tmp_path / "missing.tgz",
    ).root == _root(state_root)
    _assert_no_owned_scratch(state_root)


@pytest.mark.parametrize("valid", [True, False], ids=("valid", "invalid"))
def test_target_appearing_at_publish_is_validated_and_never_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    valid: bool,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)

    def lose_publish_race(*args: object, **kwargs: object) -> None:
        root = _root(state_root)
        if valid:
            from tests.code_kernel_helpers import create_pyright_fixture

            create_pyright_fixture(
                root,
                managed=True,
                server_bytes=artifact.server_bytes,
            )
        else:
            root.mkdir()
        raise FileExistsError("simulated publication race")

    monkeypatch.setattr(
        installer_module,
        "atomic_publish_noreplace",
        lose_publish_race,
    )

    if valid:
        result = install_pyright(state_root=state_root, artifact=artifact.path)
        assert result.root == _root(state_root)
        assert result.server_sha256 == sha256_bytes(artifact.server_bytes)
    else:
        with pytest.raises(PyrightInstallError) as error:
            install_pyright(state_root=state_root, artifact=artifact.path)
        assert _error_code(error) == "pyright_existing_install_invalid"
        assert not any(_root(state_root).iterdir())
    _assert_no_owned_scratch(state_root)


def test_a_flush_failure_hands_both_runtime_handles_to_the_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handle left as a local inside the creation helper is closed by nobody.

    `ensure_runtime_parent` creates what is missing and flushes the parent
    afterwards. Both handles have to be in the caller's hands when the flush
    fails, because the cleanup closes what it is given. On Windows a leaked
    directory handle is a sharing violation on the directory the retry needs;
    this sees the same defect on any platform.
    """
    state_root = tmp_path / "handle-state"
    state_root.mkdir()
    received: list[tuple[object, ...]] = []
    real_cleanup = installer_module.close_handles_preserving_error

    def recording_cleanup(primary_error, *handles):
        received.append(handles)
        return real_cleanup(primary_error, *handles)

    def failing_flush(_handle):
        raise OSError("directory flush failed")

    monkeypatch.setattr(
        installer_module, "close_handles_preserving_error", recording_cleanup
    )
    monkeypatch.setattr(
        installer_module, "checked_fsync_directory", failing_flush
    )

    with pytest.raises(OSError, match="directory flush failed"):
        installer_module.ensure_runtime_parent(state_root, deadline=float("inf"))

    assert received, "the cleanup never ran"
    handles = [handle for handle in received[0] if handle is not None]
    assert len(handles) == 2, "the created child was not handed over"


def test_an_existing_runtime_directory_needs_no_flush(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the call that creates a directory owes its parent a flush."""
    state_root = tmp_path / "existing-state"
    (state_root / "cache" / "code-tools" / "pyright").mkdir(parents=True)
    flushes: list[object] = []

    monkeypatch.setattr(
        installer_module,
        "checked_fsync_directory",
        lambda handle: flushes.append(handle),
    )

    parent = installer_module.ensure_runtime_parent(
        state_root, deadline=float("inf")
    )
    try:
        assert flushes == []
    finally:
        parent.close()
