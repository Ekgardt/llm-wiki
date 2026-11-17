from __future__ import annotations

import hashlib
import io
import os
import time
from collections.abc import Iterator
from pathlib import Path, PosixPath
from typing import BinaryIO, cast

import lsp_launch_package
import pytest
from llm_wiki import pyright_session_launch
from llm_wiki.pyright_session_launch import LaunchServerGuard, _unlink_error
from llm_wiki.pyright_session_values import BootstrapDegradation

from tests.slow_machine import SHORT_TIMEOUT

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="the guard's verified copy is the POSIX mechanism"
)

_CONTENT = b"server body\n"
_MISMATCH = "pyright_executable_digest_mismatch"


class _NoDescriptorDirectory(PosixPath):
    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        return False


@pytest.fixture
def server(tmp_path: Path) -> Path:
    path = tmp_path / "server.bin"
    path.write_bytes(_CONTENT)
    return path


@pytest.fixture
def owner(tmp_path: Path) -> Path:
    root = tmp_path / "owner"
    root.mkdir()
    return root


@pytest.fixture
def descriptor(server: Path) -> Iterator[int]:
    opened = os.open(server, os.O_RDONLY)
    yield opened
    os.close(opened)


def _guard(
    path: Path,
    owner: Path,
    *,
    native: bool = False,
    package_launch: object | None = None,
) -> LaunchServerGuard:
    return LaunchServerGuard(
        path,
        hashlib.sha256(_CONTENT).hexdigest(),
        command=("node", str(path), "--stdio"),
        owner_root=owner,
        deadline=time.monotonic() + SHORT_TIMEOUT,
        native=native,
        package_launch=package_launch,
    )


def _refuse(*args: object, **kwargs: object) -> object:
    raise lsp_launch_package.LaunchTreeError("tree refused")


def _refuse_open(*args: object, **kwargs: object) -> object:
    raise OSError("fdopen refused")


def _refuse_close() -> None:
    raise RuntimeError("close refused")


def test_unlinking_a_file_that_is_already_gone_is_no_error(tmp_path: Path) -> None:
    assert _unlink_error(tmp_path / "gone") is None


def test_an_owner_root_that_is_not_a_path_is_refused(server: Path) -> None:
    with pytest.raises(TypeError, match="owner_root must be a Path"):
        _guard(server, cast(Path, "owner"))


def test_a_server_that_is_not_a_regular_file_is_refused(owner: Path, tmp_path: Path) -> None:
    with pytest.raises(BootstrapDegradation, match=_MISMATCH):
        _guard(tmp_path, owner).__enter__()


def test_a_descriptor_that_names_another_file_is_refused(
    server: Path, owner: Path, descriptor: int
) -> None:
    with pytest.raises(BootstrapDegradation, match=_MISMATCH):
        _guard(server, owner)._check_unchanged(os.stat(owner), descriptor)


def test_a_launch_descriptor_that_is_not_the_verified_copy_is_refused(
    server: Path, owner: Path
) -> None:
    with pytest.raises(BootstrapDegradation, match=_MISMATCH):
        _guard(server, owner)._check_snapshot_launchable(
            os.stat(owner), os.stat(server), os.stat(server)
        )


def test_a_snapshot_that_cannot_be_opened_leaves_no_descriptor_behind(
    server: Path, owner: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = _guard(server, owner)
    monkeypatch.setattr(os, "fdopen", _refuse_open)

    with pytest.raises(OSError, match="fdopen refused"):
        guard._open_snapshot()
    monkeypatch.undo()
    guard.close()

    assert list(owner.iterdir()) == []


def test_a_native_launch_without_a_verified_copy_is_refused(server: Path, owner: Path) -> None:
    with pytest.raises(RuntimeError, match="native launch has no verified copy"):
        _guard(server, owner, native=True)._native_path_launch(cast(BinaryIO, io.BytesIO()))


def test_a_package_launch_without_a_verified_copy_is_refused(server: Path, owner: Path) -> None:
    with pytest.raises(RuntimeError, match="launch server guard is closed"):
        _guard(server, owner)._sealed_launch_tree(cast(BinaryIO, io.BytesIO()))


def test_a_package_tree_that_cannot_be_sealed_is_a_digest_mismatch(
    server: Path, owner: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lsp_launch_package, "create_launch_tree", _refuse)

    with pytest.raises(BootstrapDegradation, match=_MISMATCH):
        _guard(server, owner, package_launch=object()).__enter__()

    assert list(owner.iterdir()) == []


def test_without_a_descriptor_directory_no_descriptor_path_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pyright_session_launch, "Path", _NoDescriptorDirectory)

    with pytest.raises(RuntimeError, match="inherited descriptor paths are unavailable"):
        LaunchServerGuard._descriptor_path(7)


def test_a_native_server_is_executed_as_its_descriptor(server: Path, owner: Path) -> None:
    command = _guard(server, owner, native=True)._posix_launch_command(7)

    assert command[1:] == (str(server), "--stdio")
    assert command[0].endswith("/7")


def test_a_javascript_server_is_loaded_through_the_held_descriptor(
    owner: Path, tmp_path: Path
) -> None:
    script = tmp_path / "server.js"
    script.write_bytes(_CONTENT)

    command = _guard(script, owner)._posix_launch_command(7)

    assert (command[:2], command[3:]) == (
        ("node", "--eval"),
        ("--", "7", str(script), "--stdio"),
    )


def test_a_closed_guard_has_no_descriptor_to_rewind(server: Path, owner: Path) -> None:
    with pytest.raises(RuntimeError, match="launch server guard is closed"):
        _guard(server, owner)._rewound_descriptor()


def test_a_server_larger_than_the_bound_is_a_digest_mismatch(
    server: Path, owner: Path, descriptor: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pyright_session_launch, "MAX_SERVER_BYTES", 4)

    with pytest.raises(BootstrapDegradation, match=_MISMATCH):
        list(_guard(server, owner)._server_chunks(descriptor))


def test_verifying_a_guard_that_is_not_open_is_refused(server: Path, owner: Path) -> None:
    with pytest.raises(RuntimeError, match="launch server guard is not open"):
        _guard(server, owner).verify()


def test_checking_a_digest_on_a_guard_that_is_not_open_is_refused(
    server: Path, owner: Path
) -> None:
    with pytest.raises(RuntimeError, match="launch server guard is not open"):
        _guard(server, owner)._verify_digest(hashlib.sha256(_CONTENT).hexdigest())


def test_a_state_that_no_longer_matches_is_a_digest_mismatch(
    server: Path, owner: Path, descriptor: int
) -> None:
    with pytest.raises(BootstrapDegradation, match=_MISMATCH):
        _guard(server, owner)._require_unchanged_state(descriptor, (0, 0, 0, 0, 0, 0))


def test_a_block_that_failed_skips_verification_on_exit(server: Path, owner: Path) -> None:
    guard = _guard(server, owner)

    guard.__exit__(ValueError)

    assert guard._descriptor is None


def test_a_failed_close_after_a_failed_block_is_raised(
    server: Path, owner: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = _guard(server, owner)
    monkeypatch.setattr(guard, "close", _refuse_close)

    with pytest.raises(RuntimeError, match="close refused"):
        guard.__exit__(ValueError)
