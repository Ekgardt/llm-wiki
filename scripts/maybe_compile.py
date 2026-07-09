"""Concurrency-safe compile trigger.

Checks if compile is needed and no other compile is running, then spawns
compile_memory.py in a detached background process. The caller never
blocks — this script returns immediately (under 100ms).

Lock mechanism:
- Writes a PID file at $LLM_WIKI_STATE_ROOT/run/compile.pid
- On startup, checks if the PID is still alive (psutil-free, uses os.kill
  with signal 0 on POSIX or OpenProcess on Windows).
- Stale lock (process dead OR older than MAX_COMPILE_DURATION_S) is
  stolen automatically.

This is the ONLY entry point that should be called from hooks/wrappers/
schedulers. It guarantees:
  1. At most one compile runs at any time.
  2. Never blocks the caller (fire-and-forget).
  3. Quick exit if nothing to compile (state.json hash check).
  4. Self-heals from stale locks (crashed compile, killed process).

Usage:
    uv run python scripts/maybe_compile.py           # spawn if needed
    uv run python scripts/maybe_compile.py --force   # always spawn
    uv run python scripts/maybe_compile.py --status  # show lock state
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import (  # noqa: E402
    ROOT,
    STATE_ROOT,
    file_hash,
    load_state,
    spawn_detached,
)


COMPILE_SCRIPT = ROOT / "scripts" / "compile_memory.py"
LOCK_FILE = STATE_ROOT / "run" / "compile.pid"
LOG_OUT = STATE_ROOT / "logs" / "maybe-compile-last.log"
LOG_ERR = STATE_ROOT / "logs" / "maybe-compile-last.err.log"

# If a compile runs longer than this, assume it died and steal the lock.
# 30 minutes is generous — typical compile is 5-15 minutes for 25 daily logs.
MAX_COMPILE_DURATION_S = 30 * 60


def _is_pid_alive(pid: int) -> bool:
    """Cross-platform 'is this PID still running?' check."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # Windows: OpenProcess with PROCESS_QUERY_LIMITED_INFORMATION.
        # This access right is granted for any process the user can see
        # (no admin required), which is what we want for "is my own
        # previously-spawned compile still running?".
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    else:
        # POSIX: signal 0 = "is this process alive?".
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError, OverflowError, ValueError):
            # OverflowError: PID too large for the platform's pid_t.
            # ValueError: negative or otherwise invalid PID.
            return False


def _read_lock() -> dict | None:
    """Read the compile lock. Returns {pid, started_at} or None."""
    if not LOCK_FILE.exists():
        return None
    try:
        text = LOCK_FILE.read_text(encoding="utf-8").strip()
        if not text:
            return None
        # Lock format: "<pid>\n<started_at-iso8601>\n"
        lines = text.splitlines()
        if len(lines) < 2:
            return None
        return {"pid": int(lines[0]), "started_at": lines[1]}
    except (OSError, ValueError):
        return None


def _write_lock(pid: int) -> None:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(
        f"{pid}\n{datetime.now().isoformat(timespec='seconds')}\n",
        encoding="utf-8",
    )


def _try_claim_lock() -> bool:
    """Atomically create lock file (O_EXCL). Returns True if we own it."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(LOCK_FILE), flags)
    except FileExistsError:
        return False
    except OSError:
        return False
    try:
        payload = f"0\n{datetime.now().isoformat(timespec='seconds')}\n"
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    return True


def _clear_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


def _is_compile_running() -> tuple[bool, str]:
    """Check the lock. Returns (is_running, reason)."""
    lock = _read_lock()
    if not lock:
        return (False, "no lock file")
    pid = lock["pid"]
    if not _is_pid_alive(pid):
        return (False, f"stale lock (pid {pid} dead)")
    # Check timeout.
    try:
        started = datetime.fromisoformat(lock["started_at"])
        age = (datetime.now() - started).total_seconds()
        if age > MAX_COMPILE_DURATION_S:
            return (False, f"stale lock (age {int(age)}s > {MAX_COMPILE_DURATION_S}s)")
    except (ValueError, TypeError):
        # Bad timestamp — treat as stale.
        return (False, "stale lock (bad timestamp)")
    return (True, f"running pid={pid} since {lock['started_at']}")


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
    """Spawn detached compile if no other compile is running.

    Returns (spawned, reason). Never raises.
    """
    is_running, reason = _is_compile_running()
    if is_running and not force:
        return (False, f"skipped: {reason}")

    if not force and not _has_pending_work():
        return (False, "skipped: no pending work (all daily logs compiled)")

    # If lock exists but process is dead/stale, clear before exclusive claim.
    if not is_running and LOCK_FILE.exists():
        _clear_lock()

    # Atomic claim: only one concurrent caller can create the lock file.
    if not _try_claim_lock():
        # Another process claimed between our check and create.
        is_running2, reason2 = _is_compile_running()
        if is_running2 and not force:
            return (False, f"skipped: {reason2}")
        if not force:
            return (False, "skipped: lock race lost")
        _clear_lock()
        if not _try_claim_lock():
            return (False, "skipped: could not claim lock")

    pid = spawn_detached(
        [sys.executable, str(COMPILE_SCRIPT), "--trigger", "auto"],
        stdout_path=LOG_OUT,
        stderr_path=LOG_ERR,
    )
    if pid is None:
        _clear_lock()
        return (False, "spawn failed")

    # Update placeholder PID (0) with real spawned PID.
    _write_lock(pid)
    return (True, f"spawned compile pid={pid}")


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

    spawned, reason = spawn_compile_if_idle(force=args.force)
    print(f"maybe_compile: {reason}")
    return 0 if spawned or "skipped" in reason else 1


if __name__ == "__main__":
    raise SystemExit(main())
