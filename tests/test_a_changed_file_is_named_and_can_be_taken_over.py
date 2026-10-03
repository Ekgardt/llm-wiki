"""A shared file changed outside the installer is named, with the way on.

The refusal used to be the bare code `install_resource_drift`: it named neither the
file nor the way on (docs/research/2026-09-28-a-changed-file-is-named-and-can-be-taken-over.md).
A file the installer owns whole is no longer refused at all, an update replaces it
(`tests/test_an_update_replaces_what_it_owns.py`); a file it shares still is.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from install_control import InstallControlError, file_resource, install_resources


def _release(version: str) -> dict[str, object]:
    return {
        "commit_oid": "a" * 40,
        "project_version": version,
        "source_mode": "pinned_remote",
        "uv_lock_sha256": "b" * 64,
        "worktree_clean": True,
    }


def _shared(tmp_path: Path, desired: bytes):
    return file_resource(
        resource_id="shared-file",
        kind="test_value",
        path=tmp_path / "shared.conf",
        desired=desired,
    )


def _install(tmp_path: Path, version: str, resource) -> dict:
    return install_resources(
        state_root=tmp_path / "state",
        vault_root=tmp_path / "vault",
        release=_release(version),
        scheduler_backend="systemd_user",
        resources=[resource],
        control_version=2,
    )


def test_the_refusal_names_the_file_and_the_command_and_records_a_bare_code(
    tmp_path: Path,
) -> None:
    (tmp_path / "state").mkdir()
    _install(tmp_path, "4.0.0", _shared(tmp_path, b"ours\n"))
    (tmp_path / "shared.conf").write_bytes(b"ours\nthe user's line\n")

    with pytest.raises(InstallControlError) as refused:
        _install(tmp_path, "4.1.0", _shared(tmp_path, b"ours, newer\n"))

    transaction = json.loads(
        (tmp_path / "state" / "run" / "install" / "transaction.json").read_bytes()
    )
    said = str(refused.value)
    assert (
        "shared-file" in said,
        str(tmp_path / "shared.conf") in said,
        "--adopt shared-file" in said,
        transaction["error"] == {"code": refused.value.code},
    ) == (True, True, True, True)
