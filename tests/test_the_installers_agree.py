"""The two installers judge the same things the same way, and an unloaded job does not block an uninstall.

See docs/research/2026-09-25-the-installers-agree.md.
"""

from __future__ import annotations

from pathlib import Path

import install_control

INSTALLER_PS1 = (Path(__file__).resolve().parent.parent / "install.ps1").read_text(encoding="utf-8")


class _StrictLaunchd:
    """launchctl as it is: `bootout` of a job it does not have fails with 3."""

    def __init__(self, active: set[str]) -> None:
        self.active = active

    def __call__(self, command: tuple[str, ...], _input: bytes | None = None) -> tuple[int, bytes]:
        label = command[-1].rsplit("/", 1)[-1]
        if command[1] == "print":
            return (0, b"active\n") if label in self.active else (113, b"not found\n")
        if label not in self.active:
            return 3, b"Boot-out failed: 3: No such process\n"
        self.active.discard(label)
        return 0, b""


def test_a_job_launchd_no_longer_has_does_not_block_the_uninstall(tmp_path: Path) -> None:
    definitions = {"io.test.nightly.plist": b"<plist/>", "io.test.weekly.plist": b"<plist/>"}
    for name, body in definitions.items():
        (tmp_path / name).write_bytes(body)
    launchd = _StrictLaunchd({"io.test.weekly"})

    install_control._uninstall_launchd(tmp_path, definitions, launchd, "launchctl", "gui/501")

    assert (launchd.active, sorted(path.name for path in tmp_path.iterdir())) == (set(), [])


def test_the_windows_installer_checks_which_vault_the_mcp_entry_names() -> None:
    assert "Get-ClaudeMcpState -Config $claudeMcp -VaultRoot $VAULT_ROOT" in INSTALLER_PS1
    assert "points at another vault" in INSTALLER_PS1
