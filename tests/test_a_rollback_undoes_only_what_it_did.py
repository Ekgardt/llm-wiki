"""A failed update reverts only what it wrote, and a quarantine has a way out.

An update refused on a file changed outside the installer used to quarantine itself: the
revert reached that untouched file and refused it again, and `rollback` of a quarantined
update was refused by the same health check, so nothing could move the install forward or
back (docs/research/2026-09-28-a-rollback-undoes-only-what-it-did.md).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import install_control
import pytest
from install_control import (
    InstallControlError,
    ManagedResource,
    install_resources,
    rollback_resources,
    validate_install_state,
)


def _release(version: str = "4.0.0") -> dict[str, object]:
    return {
        "commit_oid": "a" * 40,
        "project_version": version,
        "source_mode": "pinned_remote",
        "uv_lock_sha256": "b" * 64,
        "worktree_clean": True,
    }


class _Store:
    """One managed value; `after_write` runs once after the next write that sets a value."""

    def __init__(self) -> None:
        self.value: bytes | None = None
        self.after_write = None

    def write(self, updated: bytes | None) -> None:
        self.value = updated
        hook, self.after_write = self.after_write, None
        if hook is not None and updated is not None:
            hook()


def _managed(name: str, store: _Store, desired: bytes) -> ManagedResource:
    return ManagedResource(
        resource_id=name,
        kind="test_value",
        locator=f"test://{name}",
        desired=desired,
        read_owned=lambda: store.value,
        write_owned=store.write,
        recognizes=lambda current: current in {b"old", b"new"},
    )


def _install(state_root: Path, version: str, resources: list[ManagedResource]) -> dict:
    return install_resources(
        state_root=state_root,
        vault_root=state_root.parent / "vault",
        release=_release(version),
        scheduler_backend="cron",
        resources=resources,
        control_version=2,
    )


def _transaction(state_root: Path) -> dict:
    path = state_root / "run" / "install" / "transaction.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _two_installed(tmp_path: Path) -> tuple[Path, _Store, _Store]:
    state_root = tmp_path / "state"
    state_root.mkdir()
    first, second = _Store(), _Store()
    _install(
        state_root, "4.0.0", [_managed("first", first, b"old"), _managed("second", second, b"old")]
    )
    return state_root, first, second


def _update(state_root: Path, first: _Store, second: _Store) -> None:
    _install(
        state_root, "4.1.0", [_managed("first", first, b"new"), _managed("second", second, b"new")]
    )


def test_a_drifted_resource_never_written_is_left_alone_and_the_update_reverts(
    tmp_path: Path,
) -> None:
    state_root, first, second = _two_installed(tmp_path)
    second.value = b"user-edit"

    with pytest.raises(InstallControlError, match="install_resource_drift"):
        _update(state_root, first, second)

    transaction = _transaction(state_root)
    assert (transaction["state"], transaction["error"]) == (
        "reverted",
        {"code": "install_resource_drift"},
    )
    assert (first.value, second.value) == (b"old", b"user-edit")
    assert validate_install_state(state_root)["status"] == "active"


def _quarantine(tmp_path: Path) -> tuple[Path, _Store, _Store]:
    """Someone edits `first` right after the update writes it; `second` was edited before.

    The update fails verifying `first`, never reaches `second`, and its revert refuses
    `first`, which no longer holds what the update wrote.
    """
    state_root, first, second = _two_installed(tmp_path)
    second.value = b"user-edit"

    def edit_first() -> None:
        first.value = b"edited-after-write"

    first.after_write = edit_first
    with pytest.raises(InstallControlError, match="install_rollback_quarantined"):
        _update(state_root, first, second)
    return state_root, first, second


def test_a_quarantine_keeps_the_forward_failure_as_its_cause(tmp_path: Path) -> None:
    state_root, first, second = _quarantine(tmp_path)

    transaction = _transaction(state_root)
    assert (transaction["state"], transaction["error"]) == (
        "quarantined",
        {"code": "install_rollback_drift", "cause": "install_resource_verification_failed"},
    )
    assert (first.value, second.value) == (b"edited-after-write", b"user-edit")


def test_rollback_leaves_a_quarantined_update_once_its_files_hold_what_it_wrote(
    tmp_path: Path,
) -> None:
    state_root, first, second = _quarantine(tmp_path)
    first.value = b"new"

    result = rollback_resources(
        state_root=state_root,
        resources=[_managed("first", first, b"new"), _managed("second", second, b"new")],
    )

    assert (result["state"], first.value, second.value) == ("reverted", b"old", b"user-edit")
    assert validate_install_state(state_root)["status"] == "active"


def test_an_adopted_resource_is_updated_and_its_current_content_is_the_rollback(
    tmp_path: Path,
) -> None:
    state_root, first, second = _two_installed(tmp_path)
    second.value = b"user-edit"
    adopted = replace(_managed("second", second, b"new"), adopt_current=True)

    _install(state_root, "4.1.0", [_managed("first", first, b"new"), adopted])
    updated = (first.value, second.value)
    rollback_resources(state_root=state_root, resources=[_managed("first", first, b"new"), adopted])

    assert (updated, first.value, second.value) == ((b"new", b"new"), b"old", b"user-edit")


@pytest.mark.parametrize(
    ("kind", "adopt", "code"),
    [
        ("cron_scheduler", ("second",), "install_adopt_unsupported"),
        ("test_value", ("missing",), "install_adopt_unknown_resource"),
    ],
)
def test_adoption_is_refused_for_a_scheduler_or_a_resource_not_installed(
    kind: str, adopt: tuple[str, ...], code: str
) -> None:
    resource = replace(_managed("second", _Store(), b"new"), kind=kind)

    with pytest.raises(InstallControlError, match=code):
        install_control._marked_adopted([resource], adopt)


def test_both_installers_hand_an_adopted_resource_to_the_ownership_transaction() -> None:
    """The operator's way past a refused file is the installer's own flag, on either OS."""
    root = Path(__file__).parents[1]
    shell = (root / "install.sh").read_text(encoding="utf-8")
    powershell = (root / "install.ps1").read_text(encoding="utf-8")

    forwarded = (
        'ADOPT_ARGS+=(--adopt "$argument")' in shell,
        '${ADOPT_ARGS[@]+"${ADOPT_ARGS[@]}"})"' in shell,
        '$installControlArgs += @("--adopt", $resourceId)' in powershell,
        '$reexecArguments += @("-Adopt", $resourceId)' in powershell,
    )
    assert forwarded == (True, True, True, True)
