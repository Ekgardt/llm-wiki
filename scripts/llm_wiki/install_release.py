"""The identity of the release being installed."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from install_state import (
    InstallControlError,
)
from llm_wiki.install_records import default_command_runner


def _required_command_output(command: tuple[str, ...], code: str) -> bytes:
    exit_code, output = default_command_runner(command, None)
    if exit_code != 0:
        raise InstallControlError(code)
    return output


def _project_version(root: Path) -> str:
    content = (Path(root) / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', content, re.MULTILINE)
    if match is None:
        raise InstallControlError("install_project_version_missing")
    return match.group(1)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _source_mode() -> str:
    if os.environ.get("LLM_WIKI_INSTALLER_CREATED_CLONE") == "1":
        return "pinned_remote"
    return "local_checkout"


def build_release_identity(root: Path) -> dict[str, object]:
    root = Path(root).resolve()
    commit = (
        _required_command_output(
            ("git", "-C", str(root), "rev-parse", "HEAD"),
            "install_release_identity_failed",
        )
        .decode("ascii", errors="strict")
        .strip()
        .lower()
    )
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit) is None:
        raise InstallControlError("install_release_identity_invalid")
    status = _required_command_output(
        ("git", "-C", str(root), "status", "--porcelain"),
        "install_release_status_failed",
    )
    return {
        "commit_oid": commit,
        "project_version": _project_version(root),
        "source_mode": _source_mode(),
        "uv_lock_sha256": _file_sha256(root / "uv.lock"),
        "worktree_clean": not bool(status.strip()),
    }
