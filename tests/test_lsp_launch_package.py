"""Session/profile integration retains the package launch strategy contract."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from lsp_server_profile import PackageLaunch

from tests.modules.lsp_launch_package.test_contract import _SERVER_BYTES, _digest, _launch
from tests.slow_machine import LONG_TIMEOUT

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the package launch is the POSIX branch only")

def test_pyright_still_takes_the_descriptor_launch() -> None:
    """The complete closure survives for the server whose shape allows it."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from lsp_profiles import PYRIGHT_PROFILE, TYPESCRIPT_PROFILE

    assert PYRIGHT_PROFILE.package_launch is None
    assert TYPESCRIPT_PROFILE.package_launch is not None


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
        deadline=time.monotonic() + LONG_TIMEOUT,
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
