"""The on-disk lock that keeps one compile running at a time."""
from __future__ import annotations

import os
import secrets
import time
from datetime import datetime
from pathlib import Path

import process_liveness
from llm_wiki import runtime_roots
from llm_wiki.runtime_publication import write_all
from memory_state import atomic_write, retire_stale_lock


def compile_lock_file() -> Path:
    return runtime_roots.state_root() / "run" / "compile.pid"


# A compile holds its lock for as long as its process lives. A lock is stale
# when its process is dead, when the PID-0 placeholder outlived the spawn
# window, or when the file cannot be parsed — never because it is old
# (PostgreSQL's postmaster.pid and flock(2) decide the same way). Research:
# docs/research/2026-09-10-a-lock-lives-as-long-as-its-process-not-thirty-minutes.md
PID0_TTL_SECONDS = 10.0


def _lock_bytes() -> bytes | None:
    try:
        return compile_lock_file().read_bytes()
    except OSError:
        return None


def read_lock() -> dict | None:
    """The lock as {pid, started_at, owner, identity}, or None when unreadable.

    The optional third line is a random owner token written by
    `write_lock`/`try_claim_lock`; older 2-line lock files have owner=None.
    The optional fourth line is the owner's process start identity, so a PID
    handed on to another process is not mistaken for this compile. Research:
    docs/research/2026-09-17-a-lock-names-the-process-not-only-its-number.md
    """
    payload = _lock_bytes()
    if payload is None:
        return None
    return _parse_lock(payload.decode("utf-8", errors="replace").strip().splitlines())


def _parse_lock(lines: list[str]) -> dict | None:
    if len(lines) < 2:
        return None
    try:
        pid = int(lines[0])
    except ValueError:
        return None
    return {
        "pid": pid,
        "started_at": lines[1],
        "owner": _lock_line(lines, 2) or None,
        "identity": _lock_line(lines, 3),
    }


def _lock_line(lines: list[str], index: int) -> str:
    """The optional line at `index`; empty when a shorter lock file has none."""
    if len(lines) <= index:
        return ""
    return lines[index].strip()


def _identity_of(pid: int) -> str:
    """The start identity of a running process, empty when unsettled or gone."""
    try:
        return process_liveness.process_start_identity(pid) or ""
    except (OSError, ValueError):
        return ""


def file_age(path: Path) -> float:
    """Seconds since `path` was last written; a missing file is infinitely old."""
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return float("inf")


def write_lock(pid: int, token: str | None = None) -> None:
    """Record the lock for `pid`, naming that process and keeping `token` if given.

    The spawner writes the child's PID here, so the identity recorded is the
    child's; keeping the placeholder token lets the child recognise the lock
    written for it whichever of the two gets there first.
    """
    compile_lock_file().parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    owner = token or secrets.token_hex(8)
    atomic_write(compile_lock_file(), f"{pid}\n{stamp}\n{owner}\n{_identity_of(pid)}\n")


def try_claim_lock() -> bool:
    """Create the lock with its content in one step. Returns True if we own it.

    The payload is written to a private file and linked to the lock name, so no
    reader ever sees an empty lock and retires it as unreadable. See
    `docs/research/2026-09-14-the-small-integrity-gaps.md`.
    """
    compile_lock_file().parent.mkdir(parents=True, exist_ok=True)
    payload = f"0\n{datetime.now().isoformat(timespec='seconds')}\n{secrets.token_hex(8)}\n".encode()
    staged = compile_lock_file().with_name(f".{compile_lock_file().name}.{secrets.token_hex(8)}.tmp")
    staged.write_bytes(payload)
    try:
        return _link_lock(staged, payload)
    finally:
        staged.unlink(missing_ok=True)


def _link_lock(staged: Path, payload: bytes) -> bool:
    try:
        os.link(staged, compile_lock_file())
    except FileExistsError:
        return False
    except OSError:
        return exclusive_write_lock(payload)
    return True


# Untranslated: the four lines written are the four lines read back.
_LOCK_WRITE_FLAGS = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)


def exclusive_write_lock(payload: bytes) -> bool:
    """The fallback where hard links are unsupported: create, then write."""
    try:
        fd = os.open(str(compile_lock_file()), _LOCK_WRITE_FLAGS)
    except OSError:
        return False
    try:
        write_all(fd, payload)
    finally:
        os.close(fd)
    return True


def _lock_age(lock: dict) -> float | None:
    try:
        started = datetime.fromisoformat(lock.get("started_at", ""))
    except (ValueError, TypeError):
        return None
    return (datetime.now() - started).total_seconds()


def _placeholder_state(lock: dict) -> tuple[str, str]:
    """A PID-0 placeholder is live only inside its spawn window."""
    age = _lock_age(lock)
    if age is None:
        return ("stale", "stale lock (pid-0, bad timestamp)")
    if age > PID0_TTL_SECONDS:
        return ("stale", f"stale lock (pid-0 placeholder expired, age {int(age)}s)")
    return ("live", f"spawning since {lock['started_at']}")


def _owner_state(lock: dict) -> tuple[str, str]:
    pid = lock["pid"]
    if pid == 0:
        return _placeholder_state(lock)
    if not process_liveness.owner_alive(pid, lock.get("identity")):
        return ("stale", f"stale lock (pid {pid} dead)")
    return ("live", f"running pid={pid} since {lock['started_at']}")


def lock_state() -> tuple[str, str]:
    """One verdict for every reader: ('absent'|'stale'|'live', reason)."""
    if not compile_lock_file().exists():
        return ("absent", "no lock file")
    lock = read_lock()
    if lock is None:
        return _unparsed_state()
    return _owner_state(lock)


def _unparsed_state() -> tuple[str, str]:
    """An empty lock inside the spawn window is a write in progress, not a ruin.

    Where hard links are unsupported the lock is created and then written, so
    there is a moment in which it is empty and its writer is very much alive.
    """
    empty = not (_lock_bytes() or b"").strip()
    if empty and file_age(compile_lock_file()) <= PID0_TTL_SECONDS:
        return ("live", "lock being written")
    return ("stale", "stale lock (unreadable)")


def lock_owner_token() -> str | None:
    """The owner token the lock carries now; the claimant keeps it (audit OPS-22)."""
    lock = read_lock()
    return None if lock is None else lock.get("owner")


def clear_lock(owner: str | None = None) -> bool:
    """Remove the lock unless a live process we do not own holds it.

    `owner` is the token the caller received when it claimed the lock; a
    live lock with another token belongs to someone else. Returns True when
    the lock is gone (cleared or absent). The removal retires exactly the
    bytes that were judged: a lock replaced meanwhile by a fresh owner is
    left alone (audit OPS-07).
    """
    judged = _lock_bytes()
    if judged is None:
        return not compile_lock_file().exists()
    if _judged_state(judged) == "live" and not _held_with(judged, owner):
        return False
    removed = retire_stale_lock(compile_lock_file(), judged)
    return removed or not compile_lock_file().exists()


def _held_with(judged: bytes, owner: str | None) -> bool:
    lock = _parse_lock(judged.decode("utf-8", errors="replace").strip().splitlines())
    return bool(owner) and lock is not None and lock.get("owner") == owner


def _judged_state(judged: bytes) -> str:
    """Judge ownership, preserving an unparsed lock inside its write window."""
    lock = _parse_lock(judged.decode("utf-8", errors="replace").strip().splitlines())
    if lock is None:
        return _unparsed_state()[0]
    return _owner_state(lock)[0]


def is_compile_running() -> tuple[bool, str]:
    """Check the lock. Returns (is_running, reason)."""
    state, reason = lock_state()
    return (state == "live", reason)


def claim_lock() -> bool:
    """Clear a stale lock, then claim atomically; only one caller can create it."""
    if compile_lock_file().exists():
        clear_lock()
    return try_claim_lock()


def process_status() -> dict:
    """Inspect process ownership without reading sources or receipt history."""
    is_running, reason = is_compile_running()
    return {
        "compile_running": is_running,
        "reason": reason,
        "lock_file": str(compile_lock_file()),
    }
