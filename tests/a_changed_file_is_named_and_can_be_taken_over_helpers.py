"""Helpers shared by test_a_changed_file_is_named_and_can_be_taken_over.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path

from install_state import file_resource
from llm_wiki.install_v2_transactions import install_resources


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
