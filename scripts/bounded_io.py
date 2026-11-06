"""Bounded, no-follow, stable reads for compile-time authoritative files."""
from __future__ import annotations

import math
import os
import stat
import time
from pathlib import Path

# The ceiling for one Markdown page under `knowledge/`, declared once: every
# reader of a page (journal, claim tree, guardrails snapshot, corpus, search,
# compile after-image, index rebuild, backlink repair, access telemetry)
# aliases this, so a page one of them accepts is never one another refuses.
# Measured 2026-09-09: a 4.2 MB journal over a 4 MiB reader cap stopped every
# compile for three days. Research:
# docs/research/2026-09-10-one-page-ceiling-for-every-reader-of-knowledge.md
MAX_KNOWLEDGE_PAGE_BYTES = 8 * 1024 * 1024
# One read or hash step over any bounded file. It was defined six times under
# five names before 2026-09-23; see
# `docs/research/2026-09-23-one-limit-one-place.md`.
IO_CHUNK_BYTES = 64 * 1024
# How old a file's change stamp must be before an unchanged stat proves the
# file unchanged. Operating systems stamp files from a coarse clock: Linux
# advances it once per jiffy (10 ms at the lowest supported CONFIG_HZ of 100),
# Windows once per system timer tick (15.6 ms by default). A second write in
# the same tick carries the same stamp, so an observation is trusted only once
# the stamp is older than a tick (git's racily-clean rule). Research:
# `docs/research/2026-11-06-a-rewrite-in-the-same-tick-is-a-change.md`.
TIMESTAMP_SETTLE_NS = 20_000_000
# How long a reader keeps looking at a file that is still being written before
# it refuses it: fifty settle windows, enough for a burst of writes to stop, and
# well inside the 60-second default timeout of an agent hook. A caller's earlier
# deadline ends the wait sooner; a later one leaves the rest of its time to the
# caller's other work.
SETTLE_BUDGET_NS = 1_000_000_000


class SourceChangedDuringRead(PermissionError):
    """The file was written or replaced while it was being read.

    Still a `PermissionError`, so every caller refuses as before. The type is
    for the caller that must tell "somebody is writing here, try again" from
    "this path is not safe to read". Research:
    `docs/research/2026-09-17-five-failures-only-the-other-systems-showed.md`.
    """


def _validated_deadline(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return _finite_deadline(deadline)


def _finite_deadline(deadline: object) -> float:
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise TypeError("deadline must be a monotonic timestamp or None")
    if not math.isfinite(deadline):
        raise ValueError("deadline must be finite")
    return float(deadline)


def _check_deadline(deadline: float | None, label: str) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError(f"{label} read deadline expired")


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


def _require_byte_limit(max_bytes: object) -> None:
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")


def _require_safe_ancestors(path: Path, label: str, deadline: float | None) -> None:
    for parent in path.parents:
        if parent == Path(parent.anchor):
            break
        _check_deadline(deadline, label)
        if not _acceptable_ancestor(parent):
            raise PermissionError(f"{label} parent must be a regular directory")


def _require_regular_file(path: Path, before: os.stat_result, label: str) -> None:
    if (
        path.is_symlink()
        or getattr(before, "st_file_attributes", 0) & 0x400
        or not stat.S_ISREG(before.st_mode)
    ):
        raise PermissionError(f"{label} must be a regular non-symlink file")


FileIdentity = tuple[int, int, int, int, int]


def file_identity(info: os.stat_result) -> FileIdentity:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, _change_time_ns(info))


def change_stamp_ns(info: os.stat_result) -> int:
    """The stamp every write moves: ctime on POSIX, which `utime` cannot set.

    Windows reports the creation time as ctime, so mtime is its write stamp.
    """
    if os.name == "nt":
        return info.st_mtime_ns
    return info.st_ctime_ns


def _change_time_ns(info: os.stat_result) -> int:
    if os.name == "nt":
        return 0
    return info.st_ctime_ns


def settled_lstat(
    path: Path, *, label: str = "file", deadline: float | None = None
) -> os.stat_result:
    """An lstat whose change stamp no write from now on can carry.

    A stamp younger than one clock tick is looked at again until its tick has
    passed; a write meanwhile gives a newer stamp, which is waited out the same
    way. `SETTLE_BUDGET_NS`, or an earlier caller deadline, ends a file that never
    stops being written. Callers that check the path's ancestors do so after this
    returns, so the wait cannot separate that check from the stat. Research:
    `docs/research/2026-11-06-a-rewrite-in-the-same-tick-is-a-change.md`.
    """
    until, give_up = _settle_limit(deadline, label)
    observed = path.lstat()
    wait_ns = _settle_wait_ns(observed)
    while wait_ns:
        _sleep_until_settled(wait_ns, until, give_up)
        observed = path.lstat()
        wait_ns = _settle_wait_ns(observed)
    return observed


def _settle_limit(deadline: float | None, label: str) -> tuple[float, OSError]:
    """The earlier of the settle budget and the caller's deadline, and its refusal."""
    budget_ends = time.monotonic() + SETTLE_BUDGET_NS / 1_000_000_000
    if deadline is None or budget_ends <= deadline:
        return budget_ends, SourceChangedDuringRead(f"{label} is still being written")
    return deadline, TimeoutError(f"{label} read deadline expired")


def _settle_wait_ns(info: os.stat_result) -> int:
    """How long until no write can share the stamp; none for one over a tick ahead.

    A write is stamped with the current tick, so it cannot carry a stamp still
    more than a tick in the future (a clock stepped back, a synced mtime).
    """
    ahead_ns = change_stamp_ns(info) - time.time_ns()
    if ahead_ns > TIMESTAMP_SETTLE_NS:
        return 0
    return max(ahead_ns + TIMESTAMP_SETTLE_NS, 0)


def _sleep_until_settled(wait_ns: int, until: float, give_up: OSError) -> None:
    wait_seconds = wait_ns / 1_000_000_000
    if time.monotonic() + wait_seconds >= until:
        raise give_up
    time.sleep(wait_seconds)


def _require_opened_identity(
    descriptor: int, identity: FileIdentity, label: str
) -> None:
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode):
        raise PermissionError(f"{label} changed before open")
    if identity != file_identity(opened):
        raise SourceChangedDuringRead(f"{label} changed before open")


def _read_bounded_chunks(
    descriptor: int, max_bytes: int, label: str, deadline: float | None
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= max_bytes:
        _check_deadline(deadline, label)
        chunk = os.read(descriptor, min(IO_CHUNK_BYTES, max_bytes + 1 - total))
        _check_deadline(deadline, label)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    content = b"".join(chunks)
    if len(content) > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} bytes")
    return content


def _require_unchanged_after_read(
    descriptor: int,
    path: Path,
    identity: FileIdentity,
    label: str,
) -> None:
    after = os.fstat(descriptor)
    if identity != file_identity(after):
        raise SourceChangedDuringRead(f"{label} changed during read")
    _require_same_file_at_path(path, identity, label)


def _require_same_file_at_path(
    path: Path, identity: FileIdentity, label: str
) -> None:
    current = path.lstat()
    if path.is_symlink():
        raise PermissionError(f"{label} was replaced during read")
    if identity[:2] != (current.st_dev, current.st_ino):
        raise SourceChangedDuringRead(f"{label} was replaced during read")


def _read_open_descriptor(
    descriptor: int,
    path: Path,
    identity: FileIdentity,
    max_bytes: int,
    label: str,
    deadline: float | None,
) -> bytes:
    _check_deadline(deadline, label)
    _require_opened_identity(descriptor, identity, label)
    content = _read_bounded_chunks(descriptor, max_bytes, label, deadline)
    _check_deadline(deadline, label)
    _require_unchanged_after_read(descriptor, path, identity, label)
    _check_deadline(deadline, label)
    return content


def read_stable_bytes(
    path: Path,
    max_bytes: int,
    *,
    label: str = "file",
    deadline: float | None = None,
) -> bytes:
    _require_byte_limit(max_bytes)
    deadline = _validated_deadline(deadline)
    path = Path(path)
    _check_deadline(deadline, label)
    before = settled_lstat(path, label=label, deadline=deadline)
    _require_safe_ancestors(path, label, deadline)
    _require_regular_file(path, before, label)
    if before.st_size > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} bytes")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    _check_deadline(deadline, label)
    descriptor = os.open(path, flags)
    try:
        return _read_open_descriptor(
            descriptor, path, file_identity(before), max_bytes, label, deadline
        )
    finally:
        os.close(descriptor)


