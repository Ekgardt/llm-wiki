"""Nonblocking trigger for serialized background knowledge compilation.

The fixed ``run/compile.pid`` path is an OS-backed lock file, not a PID file.
Its contents and age have no ownership meaning. The compile process keeps the
descriptor locked for its full run; the OS releases ownership after exit or
termination while the file itself remains in place.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import (  # noqa: E402
    ROOT,
    STATE_ROOT,
    compile_file_lock,
    file_hash,
    load_state,
    spawn_detached,
    trusted_compiled_daily_hashes,
)

COMPILE_SCRIPT = ROOT / "scripts" / "compile_memory.py"
LOCK_FILE = STATE_ROOT / "run" / "compile.pid"
LOG_OUT = STATE_ROOT / "logs" / "maybe-compile-last.log"
LOG_ERR = STATE_ROOT / "logs" / "maybe-compile-last.err.log"


def _is_compile_running() -> tuple[bool, str]:
    """Probe the OS lock without assigning meaning to file contents."""
    try:
        with compile_file_lock(LOCK_FILE, timeout=0):
            pass
    except TimeoutError:
        return (True, "compile lock held")
    except OSError as exc:
        return (True, f"compile lock unavailable: {exc}")
    return (False, "compile lock idle")


def _has_pending_work() -> bool:
    """Return whether a daily log hash differs from the compiled state."""
    state = load_state()
    if state.get("compile_index_pending"):
        return True
    compiled_hashes = trusted_compiled_daily_hashes(state, root=ROOT)
    daily_dir = ROOT / "knowledge" / "daily"
    if not daily_dir.exists():
        return False
    return any(
        compiled_hashes.get(path.name) != file_hash(path)
        for path in daily_dir.glob("*.md")
    )


def spawn_compile_if_idle(force: bool = False) -> tuple[bool, str]:
    """Spawn a detached compile when work is pending and no lock is held."""
    is_running, reason = _is_compile_running()
    if is_running:
        if force:
            return (False, f"skipped: live lock (force refused): {reason}")
        return (False, f"skipped: {reason}")

    if os.environ.get("MEMORY_LLM_PROVIDER", "").lower().strip() == "opencode-sdk":
        from memory_queue import ensure_compile_task

        try:
            control = ensure_compile_task()
        except (OSError, RuntimeError) as exc:
            return (False, f"compile queue failed: {exc}")
        if not control["pending"]:
            return (False, "skipped: no pending work (all daily logs compiled)")
        action = "queued" if control["created"] else "already queued"
        return (False, f"skipped: pending compile {action} for OpenCode SDK")

    if not force and not _has_pending_work():
        return (False, "skipped: no pending work (all daily logs compiled)")

    pid = spawn_detached(
        [sys.executable, str(COMPILE_SCRIPT), "--trigger", "auto"],
        stdout_path=LOG_OUT,
        stderr_path=LOG_ERR,
    )
    if pid is None:
        return (False, "spawn failed")
    return (True, f"spawned compile pid={pid}")


def status() -> dict:
    """Return a compile status snapshot for session context."""
    is_running, reason = _is_compile_running()
    return {
        "compile_running": is_running,
        "reason": reason,
        "pending_work": _has_pending_work() if not is_running else False,
        "lock_file": str(LOCK_FILE),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force", action="store_true", help="Spawn even when no work is pending."
    )
    parser.add_argument(
        "--status", action="store_true", help="Print lock state and exit."
    )
    args = parser.parse_args()

    if args.status:
        current = status()
        print(f"compile_running: {current['compile_running']}")
        print(f"reason: {current['reason']}")
        print(f"pending_work: {current['pending_work']}")
        return 0

    spawned, reason = spawn_compile_if_idle(force=args.force)
    print(f"maybe_compile: {reason}")
    return 0 if spawned or "skipped" in reason else 1


if __name__ == "__main__":
    raise SystemExit(main())
