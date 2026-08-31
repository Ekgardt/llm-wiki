"""The launch strategy for a server that must run from inside a package root.

`CODE-08` measured that the descriptor launch -- copy aside, unlink, hand `node`
a file descriptor -- cannot execute `typescript-language-server` at all, because
its ESM entry reads `new URL('../package.json', import.meta.url)` and a
descriptor path has no directory to resolve that against. These tests pin what
replaced it and, more importantly, what did **not** change:

* Pyright still takes the descriptor launch. That is the complete TOCTOU
  closure and nothing here may weaken it.
* The bytes executed are still the digest-verified bytes, checked once more
  *through the sealed path* so what is verified is what `node` will open.
* `package.json` is authored from the profile, never copied, so no unverified
  byte of the operator-writable install root is read at exec time.

The window this reopens is written down, with its platform, in
`docs/research/2026-08-29-launching-a-verified-server-without-a-toctou-window.md`.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path

import lsp_launch_package
import pytest
from lsp_server_profile import PackageLaunch

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="the package launch is the POSIX branch only"
)

_SERVER_BYTES = b"console.log('server');\n"
_MANIFEST = {"name": "example-language-server", "version": "1.2.3", "type": "module"}


def _launch() -> PackageLaunch:
    return PackageLaunch(entry_relative=Path("lib/cli.mjs"), manifest=_MANIFEST)


def _snapshot(tmp_path: Path, payload: bytes = _SERVER_BYTES) -> tuple[Path, int]:
    """A stand-in for the verified copy the guard has already made and hashed."""
    path = tmp_path / "snapshot.tmp"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path, os.open(path, os.O_RDONLY)


def _nothing() -> None:
    """The guard's deadline checkpoint, with no deadline to enforce."""


def _create(tmp_path: Path, expected: str, payload: bytes = _SERVER_BYTES):
    owner = tmp_path / "owner"
    owner.mkdir(parents=True, exist_ok=True)
    snapshot_path, descriptor = _snapshot(tmp_path, payload)
    try:
        return lsp_launch_package.create_launch_tree(
            owner,
            _launch(),
            snapshot_path=snapshot_path,
            snapshot_descriptor=descriptor,
            expected_sha256=expected,
            checkpoint=_nothing,
        )
    finally:
        os.close(descriptor)


def _digest(payload: bytes = _SERVER_BYTES) -> str:
    return hashlib.sha256(payload).hexdigest()


def test_the_entry_runs_from_a_directory_that_holds_a_package_manifest(
    tmp_path: Path,
) -> None:
    """The whole point: a real path whose `..` contains a `package.json`."""
    tree = _create(tmp_path, _digest())
    assert tree.entry.read_bytes() == _SERVER_BYTES
    assert (tree.entry.parent.parent / "package.json").is_file()
    assert tree.entry.name == "cli.mjs"


def test_the_manifest_is_authored_not_copied(tmp_path: Path) -> None:
    """Copying the shipped one would carry unverified install-root bytes."""
    tree = _create(tmp_path, _digest())
    manifest = json.loads((tree.root / "package.json").read_text("utf-8"))
    assert manifest == _MANIFEST


def test_the_tree_is_sealed_read_only(tmp_path: Path) -> None:
    """`0500` is what stops an entry being replaced inside the directory."""
    tree = _create(tmp_path, _digest())
    entry_mode = stat.S_IMODE(tree.entry.stat().st_mode)
    directory_modes = {stat.S_IMODE(item.stat().st_mode) for item in tree.directories}
    assert entry_mode == lsp_launch_package.SEALED_FILE_MODE
    assert directory_modes == {lsp_launch_package.SEALED_DIRECTORY_MODE}


def test_the_launch_root_is_private_to_this_owner(tmp_path: Path) -> None:
    """A fresh nonce per launch, under the owner's own scratch root."""
    first = _create(tmp_path / "a", _digest())
    second = _create(tmp_path / "b", _digest())
    assert first.root.name.startswith(lsp_launch_package.LAUNCH_PREFIX)
    assert first.root.name != second.root.name


def test_bytes_that_do_not_match_the_pin_are_refused(tmp_path: Path) -> None:
    """The digest check is not weakened by moving the file into a package root."""
    with pytest.raises(lsp_launch_package.LaunchTreeError):
        _create(tmp_path, "0" * 64)


def test_a_refused_launch_leaves_nothing_behind(tmp_path: Path) -> None:
    """A half-built tree would be a sealed directory nobody ever removes."""
    with pytest.raises(lsp_launch_package.LaunchTreeError):
        _create(tmp_path, "0" * 64)
    assert list((tmp_path / "owner").iterdir()) == []


def test_the_tree_is_removed_when_the_guard_closes(tmp_path: Path) -> None:
    tree = _create(tmp_path, _digest())
    assert lsp_launch_package.remove_launch_tree(tree) is None
    assert not tree.root.exists()


def test_removing_something_that_is_not_a_tree_is_not_an_error() -> None:
    """`close()` passes whatever it holds, including nothing."""
    assert lsp_launch_package.remove_launch_tree(None) is None


def test_the_sealed_path_is_verified_not_just_the_copy(tmp_path: Path) -> None:
    """What is checked has to be what `node` will open, reached by path.

    Substituting the entry after sealing has to fail the read-back, which is the
    property that makes the sealed tree worth building at all.
    """
    tree = _create(tmp_path, _digest())
    os.chmod(tree.root / "lib", lsp_launch_package.PRIVATE_DIRECTORY_MODE)
    tree.entry.unlink()
    tree.entry.write_bytes(b"impostor\n")
    with pytest.raises(lsp_launch_package.LaunchTreeError):
        lsp_launch_package.verify_sealed_entry(
            tree, _identity_of(tree), _digest(), _nothing
        )


def _identity_of(tree: lsp_launch_package.LaunchTree) -> tuple[int, int]:
    info = tree.entry.stat()
    return (info.st_dev, info.st_ino)


def test_pyright_still_takes_the_descriptor_launch() -> None:
    """The complete closure survives for the server whose shape allows it."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from lsp_profiles import PYRIGHT_PROFILE, TYPESCRIPT_PROFILE

    assert PYRIGHT_PROFILE.package_launch is None
    assert TYPESCRIPT_PROFILE.package_launch is not None


# -- the guard chooses the strategy the profile declares --------------------


def _guard(tmp_path: Path, package_launch: PackageLaunch | None):
    from pyright_session import _LaunchServerGuard

    server = tmp_path / "install" / "package" / "lib" / "cli.mjs"
    server.parent.mkdir(parents=True)
    server.write_bytes(_SERVER_BYTES)
    owner = tmp_path / "owner"
    owner.mkdir()
    return _LaunchServerGuard(
        server,
        _digest(),
        command=("/usr/bin/node", str(server), "--stdio"),
        owner_root=owner,
        deadline=time.monotonic() + 60.0,
        degradation_prefix="typescript",
        package_launch=package_launch,
    )


def test_a_package_profile_launches_a_path_inside_its_package_root(
    tmp_path: Path,
) -> None:
    """The regression for blocker 1: not a descriptor, and not the install root."""
    guard = _guard(tmp_path, _launch())
    launch = guard.__enter__()
    try:
        entry = Path(launch.command[1])
        assert launch.command == ("/usr/bin/node", str(entry), "--stdio")
        assert (entry.parent.parent / "package.json").is_file()
        assert launch.pass_fds == ()
        assert not str(entry).startswith("/proc/")
    finally:
        guard.close()


def test_the_launch_tree_does_not_outlive_the_guard(tmp_path: Path) -> None:
    guard = _guard(tmp_path, _launch())
    launch = guard.__enter__()
    root = Path(launch.command[1]).parent.parent
    guard.close()
    assert not root.exists()


def test_a_profile_without_a_package_launch_still_gets_a_descriptor(
    tmp_path: Path,
) -> None:
    """Pyright's path, unchanged: an inherited descriptor and no launch tree."""
    guard = _guard(tmp_path, None)
    launch = guard.__enter__()
    try:
        assert len(launch.pass_fds) == 1
        assert list((tmp_path / "owner").iterdir()) == []
    finally:
        guard.close()
