"""The managed Pyright installer revalidates an existing install, refuses unsafe state roots, and keeps its CLI strict."""

from __future__ import annotations

import ast
import concurrent.futures
import dataclasses
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import install_pyright as installer_module
import pytest
from install_pyright import InstalledPyright, PyrightInstallError, install_pyright
from pyright_profile import (
    PYRIGHT_PACKAGE_INTEGRITY,
    PYRIGHT_PACKAGE_SHA256,
    PYRIGHT_VERSION,
)
from reliable_memory import sha256_bytes

from tests.install_pyright_helpers import _artifact, _root
from tests.pyright_installer_helpers import (
    _assert_no_owned_scratch,
    _error_code,
    _installer_entries,
    assert_state_validation_honors_end_to_end_deadline,
)
from tests.slow_machine import LONG_TIMEOUT


def test_valid_existing_install_is_idempotent_before_artifact_or_network_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    expected = install_pyright(state_root=state_root, artifact=artifact.path)
    artifact.path.unlink()
    inaccessible = tmp_path / "missing.tgz"
    monkeypatch.setattr(
        installer_module,
        "_open_pinned_url",
        lambda *args, **kwargs: pytest.fail("network used for idempotent install"),
    )

    observed = install_pyright(state_root=state_root, artifact=inaccessible)

    assert observed == expected
    _assert_no_owned_scratch(state_root)


def test_existing_tree_snapshot_does_not_open_ordinary_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    expected = install_pyright(state_root=state_root, artifact=artifact.path)
    ordinary = _root(state_root) / "package/ordinary"
    ordinary.mkdir()
    for index in range(32):
        (ordinary / f"module-{index:02d}.js").write_bytes(b"content")
    real_open_known_child = installer_module.open_known_child
    real_existing_entries = installer_module.existing_directory_entries
    opened_files: list[str] = []
    opened_directories: list[str] = []
    enumerations: list[tuple[str, ...]] = []

    def open_known_child(
        parent: object,
        name: str,
        *,
        directory: bool,
    ):
        if directory:
            opened_directories.append(name)
        else:
            opened_files.append(name)
            if name.startswith("module-"):
                pytest.fail("ordinary archive file was opened during idempotence")
        return real_open_known_child(parent, name, directory=directory)

    def existing_entries(directory: object):
        entries = real_existing_entries(directory)
        enumerations.append(tuple(entry.name for entry in entries))
        return entries

    monkeypatch.setattr(installer_module, "open_known_child", open_known_child)
    monkeypatch.setattr(
        installer_module,
        "existing_directory_entries",
        existing_entries,
    )

    observed = install_pyright(
        state_root=state_root,
        artifact=tmp_path / "must-not-be-read.tgz",
    )

    assert observed == expected
    assert set(opened_files) <= {
        "install-manifest.json",
        "package.json",
        "langserver.index.js",
    }
    assert len(opened_files) <= 6
    assert len(enumerations) == 3
    assert len(opened_directories) <= 2 * len(enumerations) + 2


@pytest.mark.skipif(os.name != "nt", reason="Windows enumeration complexity")
def test_windows_5424_file_idempotence_enumerates_each_directory_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    expected = install_pyright(state_root=state_root, artifact=artifact.path)
    package = _root(state_root) / "package"
    for index in range(5421):
        (package / f"synthetic-{index:04d}.js").write_bytes(b"")
    assert sum(path.is_file() for path in _root(state_root).rglob("*")) == 5424
    real_list_directory = installer_module._windows_workspace.list_directory
    enumerations = {"root": 0, "package": 0}

    def enumeration_label(names: set[str]) -> str | None:
        if names == {"install-manifest.json", "package"}:
            return "root"
        if "synthetic-0000.js" in names:
            return "package"
        return None

    def bounded_list_directory(handle: int, *, max_entries: int):
        entries = real_list_directory(handle, max_entries=max_entries)
        label = enumeration_label({entry.name for entry in entries})
        if label is None:
            return entries
        enumerations[label] += 1
        if enumerations[label] > 1:
            pytest.fail(f"existing {label} directory was enumerated more than once")
        return entries

    monkeypatch.setattr(
        installer_module._windows_workspace,
        "list_directory",
        bounded_list_directory,
    )

    started = time.monotonic()
    observed = install_pyright(
        state_root=state_root,
        artifact=tmp_path / "must-not-be-read.tgz",
    )
    elapsed = time.monotonic() - started

    assert observed == expected
    assert enumerations == {"root": 1, "package": 1}
    assert elapsed < 10.0


def test_existing_install_root_swap_before_return_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    install_pyright(state_root=state_root, artifact=artifact.path)
    root = _root(state_root)
    displaced = root.with_name("validated-root")
    real_sha256 = installer_module.hashlib.sha256
    attempted = False
    swapped = False

    def swap_after_validation(content: bytes = b""):
        nonlocal attempted, swapped
        if not swapped and content == artifact.server_bytes:
            attempted = True
            root.rename(displaced)
            root.mkdir()
            swapped = True
        return real_sha256(content)

    monkeypatch.setattr(installer_module.hashlib, "sha256", swap_after_validation)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=tmp_path / "missing.tgz")

    assert attempted is True
    assert _error_code(error) == "pyright_existing_install_invalid"
    if swapped:
        assert not any(root.iterdir())


def test_existing_install_appearing_after_lock_is_revalidated_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.code_kernel_helpers import create_pyright_fixture

    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    real_try_create_lock = installer_module.try_create_lock

    def acquire_then_publish(parent: object, deadline: float):
        lock = real_try_create_lock(parent, deadline)
        assert lock is not None
        create_pyright_fixture(
            _root(state_root),
            managed=True,
            server_bytes=artifact.server_bytes,
        )
        return lock

    monkeypatch.setattr(installer_module, "try_create_lock", acquire_then_publish)
    monkeypatch.setattr(
        installer_module,
        "copy_local_artifact",
        lambda *args, **kwargs: pytest.fail("artifact read after locked idempotent check"),
    )

    result = install_pyright(
        state_root=state_root,
        artifact=tmp_path / "missing.tgz",
    )

    assert result.root == _root(state_root)
    assert result.server_sha256 == sha256_bytes(artifact.server_bytes)


def test_existing_install_with_unsafe_extra_entry_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    install_pyright(state_root=state_root, artifact=artifact.path)
    unsafe = _root(state_root) / "package/unsafe-entry"
    unsafe.write_bytes(b"placeholder")

    if os.name == "nt":
        real_list_directory = installer_module._windows_workspace.list_directory

        def list_directory(handle: int, *, max_entries: int):
            return [
                dataclasses.replace(entry, kind="link")
                if entry.name == unsafe.name
                else entry
                for entry in real_list_directory(handle, max_entries=max_entries)
            ]

        monkeypatch.setattr(
            installer_module._windows_workspace,
            "list_directory",
            list_directory,
        )
    else:
        unsafe.unlink()
        unsafe.symlink_to(_root(state_root) / "install-manifest.json")

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=tmp_path / "must-not-be-read")

    assert _error_code(error) == "pyright_existing_install_invalid"


def _report_server_as_link(server: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Where symlinks are unavailable, make the listing report one anyway."""
    real_list_directory = installer_module._windows_workspace.list_directory

    def list_directory(handle: int, *, max_entries: int):
        return [
            dataclasses.replace(entry, kind="link")
            if entry.name == server.name
            else entry
            for entry in real_list_directory(handle, max_entries=max_entries)
        ]

    monkeypatch.setattr(
        installer_module._windows_workspace, "list_directory", list_directory
    )


def _replace_server_with_link(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = root / "package/langserver.index.js"
    server.unlink()
    try:
        server.symlink_to(root / "install-manifest.json")
    except OSError:
        server.write_bytes(b"simulated reparse point")
        _report_server_as_link(server, monkeypatch)


def _damage_installed(root: Path, damage: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Damage a completed installation in the named way."""
    if damage == "manifest":
        (root / "install-manifest.json").write_bytes(b"{}")
        return
    if damage == "server":
        (root / "package/langserver.index.js").write_bytes(b"changed")
        return
    _replace_server_with_link(root, monkeypatch)


def _damage_existing_install(
    state_root: Path,
    root: Path,
    artifact,
    damage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leave one existing installation in each way the installer must refuse."""
    if damage == "empty":
        root.mkdir(parents=True)
        return
    install_pyright(state_root=state_root, artifact=artifact.path)
    _damage_installed(root, damage, monkeypatch)


@pytest.mark.parametrize("damage", ["empty", "manifest", "server", "symlink"], ids=str)
def test_existing_invalid_or_unsafe_target_is_never_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    root = _root(state_root)
    _damage_existing_install(state_root, root, artifact, damage, monkeypatch)
    before = tuple(sorted(str(path.relative_to(root)) for path in root.rglob("*")))

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=artifact.path)

    assert _error_code(error) == "pyright_existing_install_invalid"
    assert tuple(sorted(str(path.relative_to(root)) for path in root.rglob("*"))) == before
    _assert_no_owned_scratch(state_root)


def test_scratch_an_abrupt_death_left_behind_is_swept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`.install-pyright-*` outlives a `SIGKILL`; the next install holds the lock.

    Audit 3, B22. Research:
    `docs/research/2026-09-17-inst-the-second-installer-gets-the-first-ones-guarantees.md`.
    """
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    install_pyright(state_root=state_root, artifact=artifact.path)
    abandoned = state_root / "cache/code-tools/pyright/.install-pyright-stage-dead"
    (abandoned / "package").mkdir(parents=True)
    (abandoned / "package/half-written.js").write_bytes(b"z" * 4096)
    shutil.rmtree(_root(state_root))

    install_pyright(state_root=state_root, artifact=artifact.path)

    assert not abandoned.exists()
    assert _installer_entries(state_root) == (PYRIGHT_VERSION,)


def test_concurrent_installers_converge_on_one_valid_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(install_pyright, state_root=state_root, artifact=artifact.path)
            for _ in range(2)
        ]
        # Convergence is the claim, not speed: the loser waits for the winner's
        # publication, and both together took longer than ten seconds on a
        # loaded runner.
        results = [future.result(timeout=LONG_TIMEOUT) for future in futures]

    assert results[0] == results[1]
    assert results[0].root == _root(state_root)
    assert _installer_entries(state_root) == (PYRIGHT_VERSION,)


def test_symlink_or_reparse_runtime_parent_is_rejected_before_artifact_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    cache = state_root / "cache"
    try:
        cache.symlink_to(outside, target_is_directory=True)
    except OSError:
        cache.mkdir()
        real_lstat = Path.lstat

        def lstat(path: Path):
            value = real_lstat(path)
            if path == cache:
                return SimpleNamespace(
                    st_mode=stat.S_IFDIR | 0o700,
                    st_file_attributes=0x400,
                    st_dev=value.st_dev,
                    st_ino=value.st_ino,
                    st_size=value.st_size,
                    st_mtime_ns=value.st_mtime_ns,
                )
            return value

        monkeypatch.setattr(Path, "lstat", lstat)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=tmp_path / "must-not-be-read")

    assert _error_code(error) == "pyright_state_root_unsafe"
    assert not _root(state_root).exists()


def test_known_network_state_root_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"

    def reject_network(path: Path, deadline: float) -> None:
        raise PermissionError("simulated network state root")

    monkeypatch.setattr(installer_module, "require_local_filesystem", reject_network)

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(state_root=state_root, artifact=tmp_path / "must-not-be-read")

    assert _error_code(error) == "pyright_state_root_unsafe"


def test_cloud_synchronized_state_root_is_rejected_before_artifact_access(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "OneDrive" / "state"

    with pytest.raises(PyrightInstallError) as error:
        install_pyright(
            state_root=state_root,
            artifact=tmp_path / "must-not-be-read.tgz",
        )

    assert _error_code(error) == "pyright_state_root_unsafe"
    assert not _root(state_root).exists()


def test_simulated_darwin_state_validation_uses_no_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess_calls: list[object] = []

    def forbidden_subprocess(*args: object, **kwargs: object):
        subprocess_calls.append(args)
        raise AssertionError("installer state validation invoked a subprocess")

    monkeypatch.setattr(installer_module.sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", forbidden_subprocess)
    monkeypatch.setattr(
        installer_module,
        "_known_network_path",
        lambda path: subprocess.run(["/sbin/mount"]),
        raising=False,
    )
    monkeypatch.setattr(
        installer_module,
        "darwin_filesystem_details",
        lambda path, deadline: ("apfs", "/dev/disk3s1", 0x00001000),
        raising=False,
    )

    installer_module.prepare_state_root(
        tmp_path / "state",
        time.monotonic() + 2.0,
    )

    assert subprocess_calls == []


@pytest.mark.parametrize("deadline", [True, float("nan"), float("inf"), "later"])
def test_invalid_deadline_is_rejected_before_state_creation(
    tmp_path: Path,
    deadline: object,
) -> None:
    state_root = tmp_path / "state"
    with pytest.raises(ValueError, match="deadline"):
        install_pyright(state_root=state_root, deadline=deadline)  # type: ignore[arg-type]
    assert not state_root.exists()


def test_expired_deadline_is_rejected_before_state_creation(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    with pytest.raises(TimeoutError, match="deadline"):
        install_pyright(state_root=state_root, deadline=time.monotonic() - 1)
    assert not state_root.exists()


def test_state_validation_honors_end_to_end_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert_state_validation_honors_end_to_end_deadline(tmp_path, monkeypatch)


def test_nested_install_timeout_is_never_relabelled_as_io_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _artifact(tmp_path, monkeypatch)

    def expire_during_copy(*args: object, **kwargs: object):
        raise TimeoutError("Pyright installation deadline expired")

    monkeypatch.setattr(
        installer_module,
        "copy_to_owned_file",
        expire_during_copy,
    )

    with pytest.raises(TimeoutError, match="deadline"):
        install_pyright(state_root=tmp_path / "state", artifact=artifact.path)


def test_cli_requires_absolute_state_and_artifact_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    expected = InstalledPyright(
        _root(tmp_path),
        PYRIGHT_VERSION,
        PYRIGHT_PACKAGE_SHA256,
        PYRIGHT_PACKAGE_INTEGRITY,
        "1" * 64,
        "2" * 64,
    )
    monkeypatch.setattr(
        installer_module,
        "install_pyright",
        lambda **kwargs: calls.append(kwargs) or expected,
    )

    with pytest.raises(SystemExit) as state_error:
        installer_module.main(["--state-root", "relative"])
    assert state_error.value.code == 2
    with pytest.raises(SystemExit) as artifact_error:
        installer_module.main(
            ["--state-root", str(tmp_path), "--artifact", "relative.tgz"]
        )
    assert artifact_error.value.code == 2

    assert installer_module.main(
        ["--state-root", str(tmp_path), "--artifact", str(tmp_path / "artifact.tgz")]
    ) == 0
    assert calls == [{"state_root": tmp_path, "artifact": tmp_path / "artifact.tgz"}]


def test_installer_source_has_no_archive_extract_npm_subprocess_or_shell_calls() -> None:
    source = Path(installer_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_attributes = {"extract", "extractall", "unpack_archive"}
    assert "subprocess" not in source
    assert "npm" not in source.casefold()
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden_attributes
        for node in ast.walk(tree)
    )
