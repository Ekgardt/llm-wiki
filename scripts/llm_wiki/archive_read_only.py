"""Whether an archived bag member is read-only for its owner, on POSIX and on Windows."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def archive_path_is_read_only(path: Path) -> bool:
    if os.name == "nt":
        return _windows_path_is_read_only(path)
    expected = 0o500 if path.is_dir() else 0o400
    try:
        return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) == expected
    except OSError:
        return False


def _windows_path_is_read_only(path: Path) -> bool:
    from llm_wiki.windows_acl import (  # noqa: E402
        acl_output_text,
        run_acl_command,
        windows_acl_identity,
    )

    verified = run_acl_command(["icacls", str(path)])
    if verified.returncode != 0:
        return False
    return _acl_is_owner_read_only(
        acl_output_text(verified.stdout), windows_acl_identity()
    )


def _acl_is_owner_read_only(acl: str, identity: str) -> bool:
    lines = _acl_entry_lines(acl)
    if not lines or _acl_grants_write(acl):
        return False
    return all(identity.casefold() in line.casefold() for line in lines)


def _acl_entry_lines(acl: str) -> list[str]:
    return [line.strip() for line in acl.splitlines() if ":(" in line]


def _acl_grants_write(acl: str) -> bool:
    return any(marker in acl for marker in ("(F)", "(M)", "(W)"))

