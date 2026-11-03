"""Shared ancestor metadata policy; callers retain their own race protection."""
from __future__ import annotations

import os
import stat
from pathlib import Path


def _root_controlled(info: os.stat_result) -> bool:
    """Only an administrator can write here."""
    return info.st_uid == 0 and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def _system_symlink(link: Path) -> bool:
    """A symlink an ordinary user could not have created or replaced.

    Traversal-resistant APIs draw the line here rather than at "no symlink
    anywhere": `openat2(RESOLVE_BENEATH)`, FreeBSD's `O_RESOLVE_BENEATH` and
    Go's `os.Root` all defend against constructs an unprivileged user can make
    and state plainly that ones requiring root are outside the threat model.
    macOS ships `/var` as a root-owned symlink to `/private/var`, so every
    temporary file on that platform sits behind one; refusing them all made
    this reader unusable there. A symlink qualifies only when both it and the
    directory holding it belong to root and are not group- or other-writable,
    which is exactly the "an administrator put it there" case. Symlink mode
    bits are meaningless on Linux, so the containing directory carries the
    decision.
    """
    if os.name == "nt":
        return False
    try:
        link_info = link.lstat()
        parent_info = link.parent.stat()
    except OSError:
        return False
    if link_info.st_uid != 0:
        return False
    return _root_controlled(parent_info)


def _acceptable_ancestor(parent: Path) -> bool:
    """Every ancestor is a real directory, or a system-owned symlink to one."""
    info = parent.lstat()
    if getattr(info, "st_file_attributes", 0) & 0x400:
        return False
    if parent.is_symlink():
        return _system_symlink(parent) and parent.is_dir()
    return stat.S_ISDIR(info.st_mode)
