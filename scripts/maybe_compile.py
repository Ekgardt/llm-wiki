"""Concurrency-safe compile trigger.

Checks if compile is needed and no other compile is running, then spawns
compile_memory.py in a detached background process. The caller never
blocks — this script returns immediately (under 100ms).

Lock mechanism:
- Writes a PID file at $LLM_WIKI_STATE_ROOT/run/compile.pid
- On startup, checks if the PID is still alive (psutil-free, uses os.kill
  with signal 0 on POSIX or OpenProcess on Windows).
- A stale lock (process dead, or a PID-0 placeholder past its spawn
  window) is cleared automatically; a live process holds its lock however
  old the file is.

This is the ONLY entry point that should be called from hooks/wrappers/
schedulers. It guarantees:
  1. At most one compile runs at any time.
  2. Never blocks the caller (fire-and-forget).
  3. Quick exit if nothing to compile (state.json hash check).
  4. Clears a stale lock (crashed compile, killed process) by process liveness, never by age.

Usage:
    uv run python scripts/maybe_compile.py           # spawn if needed
    uv run python scripts/maybe_compile.py --force   # always spawn
    uv run python scripts/maybe_compile.py --status  # show lock state
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import operational_ownership as _operational_ownership  # noqa: E402
from memory_state import (  # noqa: E402
    ROOT,
    STATE_ROOT,
    atomic_write,
    file_hash,
    load_state,
    retire_stale_lock,
    spawn_detached,
)
from memory_state import (  # noqa: E402
    _is_pid_alive as _os_pid_alive,
)

acquire_compile_owner = _operational_ownership.acquire_compile_owner


def release_marker_owner(lease, marker) -> None:
    _operational_ownership.release_marker_owner(lease, marker)

COMPILE_SCRIPT = ROOT / "scripts" / "compile_memory.py"
LOCK_FILE = STATE_ROOT / "run" / "compile.pid"
LOG_OUT = STATE_ROOT / "logs" / "maybe-compile-last.log"
LOG_ERR = STATE_ROOT / "logs" / "maybe-compile-last.err.log"

# A compile holds its lock for as long as its process lives. A lock is stale
# when its process is dead, when the PID-0 placeholder outlived the spawn
# window, or when the file cannot be parsed — never because it is old
# (PostgreSQL's postmaster.pid and flock(2) decide the same way). Research:
# docs/research/2026-09-10-a-lock-lives-as-long-as-its-process-not-thirty-minutes.md
_PID0_TTL_SECONDS = 10.0

def _is_pid_alive(pid: int) -> bool:
    """Cross-platform 'is this PID still running?' check; PID 0 is the placeholder."""
    if pid == 0:
        return True
    return _os_pid_alive(pid)


def _lock_bytes() -> bytes | None:
    try:
        return LOCK_FILE.read_bytes()
    except OSError:
        return None


def _read_lock() -> dict | None:
    """The lock as {pid, started_at, owner}, or None when absent or unreadable.

    The optional third line is a random owner token written by
    `_write_lock`/`_try_claim_lock`; older 2-line lock files have owner=None.
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
    owner = lines[2].strip() if len(lines) >= 3 else ""
    return {"pid": pid, "started_at": lines[1], "owner": owner or None}


def _write_lock(pid: int) -> None:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        LOCK_FILE,
        f"{pid}\n{datetime.now().isoformat(timespec='seconds')}\n{secrets.token_hex(8)}\n",
    )


def _try_claim_lock() -> bool:
    """Atomically create lock file (O_EXCL). Returns True if we own it."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(LOCK_FILE), flags)
    except OSError:
        return False
    try:
        payload = f"0\n{datetime.now().isoformat(timespec='seconds')}\n{secrets.token_hex(8)}\n"
        os.write(fd, payload.encode("utf-8"))
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
    if age > _PID0_TTL_SECONDS:
        return ("stale", f"stale lock (pid-0 placeholder expired, age {int(age)}s)")
    return ("live", f"spawning since {lock['started_at']}")


def _owner_state(lock: dict) -> tuple[str, str]:
    pid = lock["pid"]
    if pid == 0:
        return _placeholder_state(lock)
    if not _is_pid_alive(pid):
        return ("stale", f"stale lock (pid {pid} dead)")
    return ("live", f"running pid={pid} since {lock['started_at']}")


def _lock_state() -> tuple[str, str]:
    """One verdict for every reader: ('absent'|'stale'|'live', reason)."""
    if not LOCK_FILE.exists():
        return ("absent", "no lock file")
    lock = _read_lock()
    if lock is None:
        return ("stale", "stale lock (unreadable)")
    return _owner_state(lock)


def lock_owner_token() -> str | None:
    """The owner token the lock carries now; the claimant keeps it (audit OPS-22)."""
    lock = _read_lock()
    return None if lock is None else lock.get("owner")


def _clear_lock(owner: str | None = None) -> bool:
    """Remove the lock unless a live process we do not own holds it.

    `owner` is the token the caller received when it claimed the lock; a
    live lock with another token belongs to someone else. Returns True when
    the lock is gone (cleared or absent). The removal retires exactly the
    bytes that were judged: a lock replaced meanwhile by a fresh owner is
    left alone (audit OPS-07).
    """
    judged = _lock_bytes()
    if judged is None:
        return not LOCK_FILE.exists()
    if _judged_state(judged) == "live" and not _held_with(judged, owner):
        return False
    removed = retire_stale_lock(LOCK_FILE, judged)
    return removed or not LOCK_FILE.exists()


def _held_with(judged: bytes, owner: str | None) -> bool:
    lock = _parse_lock(judged.decode("utf-8", errors="replace").strip().splitlines())
    return bool(owner) and lock is not None and lock.get("owner") == owner


def _judged_state(judged: bytes) -> str:
    """absent/stale/live for the exact bytes that will be retired; unreadable is stale."""
    lock = _parse_lock(judged.decode("utf-8", errors="replace").strip().splitlines())
    if lock is None:
        return "stale"
    return _owner_state(lock)[0]


def _is_compile_running() -> tuple[bool, str]:
    """Check the lock. Returns (is_running, reason)."""
    state, reason = _lock_state()
    return (state == "live", reason)


def _has_pending_work() -> bool:
    """Quick check: are there daily logs whose hash differs from last compile?

    Reads state.json (cheap) and compares against current daily files.
    Under 50ms even for 100 daily logs.
    """
    state = load_state()
    compiled_hashes = state.get("compiled_daily_hashes", {}) or {}
    daily_dir = ROOT / "knowledge" / "daily"
    if not daily_dir.exists():
        return False
    for p in daily_dir.glob("*.md"):
        if compiled_hashes.get(p.name) != file_hash(p):
            return True
    return False


def spawn_compile_if_idle(force: bool = False) -> tuple[bool, str]:
    """Spawn a detached compile unless one is running or nothing is pending.

    Returns (spawned, reason). Never raises. ``force`` bypasses the
    "no pending work" gate but never steals a live lock.
    """
    spawned, _skipped, reason = _spawn_outcome(force)
    return (spawned, reason)


def _spawn_outcome(force: bool) -> tuple[bool, bool, str]:
    """(spawned, skipped, reason): a skip is a named refusal, not a failure."""
    refusal = _refusal_before_claim(force)
    if refusal is not None:
        return (False, True, refusal)
    if not _claim_lock():
        # Another caller took the lock between our check and our claim; name
        # what holds it now rather than a race the reader cannot verify.
        _state, reason = _lock_state()
        return (False, True, f"skipped: {reason}")
    return _spawn_claimed()


def _refusal_before_claim(force: bool) -> str | None:
    is_running, reason = _is_compile_running()
    if is_running:
        return _live_lock_refusal(force, reason)
    if not force and not _has_pending_work():
        return "skipped: no pending work (all daily logs compiled)"
    return None


def _live_lock_refusal(force: bool, reason: str) -> str:
    if not force:
        return f"skipped: {reason}"
    print(
        "maybe_compile: WARNING — force-stealing a live lock can "
        "cause races; refusing to proceed.",
        file=sys.stderr,
    )
    return f"skipped: live lock (force refused): {reason}"


def _claim_lock() -> bool:
    """Clear a stale lock, then claim atomically; only one caller can create it."""
    if LOCK_FILE.exists():
        _clear_lock()
    return _try_claim_lock()


def _spawn_claimed() -> tuple[bool, bool, str]:
    # The placeholder's token is ours to clear if the spawn fails.
    placeholder = lock_owner_token()
    pid = spawn_detached(
        [sys.executable, str(COMPILE_SCRIPT), "--trigger", "auto"],
        stdout_path=LOG_OUT,
        stderr_path=LOG_ERR,
    )
    if pid is None:
        _clear_lock(placeholder)
        return (False, False, "spawn failed")
    # Replace the placeholder PID (0) with the real one; the child owns it now.
    _write_lock(pid)
    return (True, False, f"spawned compile pid={pid}")


def status() -> dict:
    """Snapshot for the metacognitive block."""
    is_running, reason = _is_compile_running()
    return {
        "compile_running": is_running,
        "reason": reason,
        "pending_work": _has_pending_work() if not is_running else False,
        "lock_file": str(LOCK_FILE),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true", help="Spawn even if lock held or no work.")
    p.add_argument("--status", action="store_true", help="Print lock state and exit.")
    args = p.parse_args()

    if args.status:
        s = status()
        print(f"compile_running: {s['compile_running']}")
        print(f"reason: {s['reason']}")
        print(f"pending_work: {s['pending_work']}")
        return 0

    spawned, skipped, reason = _spawn_outcome(args.force)
    print(f"maybe_compile: {reason}")
    if spawned or skipped:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
