"""Concurrency-safe compile trigger.

Checks if compile is needed and no other compile is running, then spawns
compile_memory.py in a detached background process. Admission reads complete
context receipts; a caller may provide its deadline for that work.

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
  2. Background compilation begins only after foreground admission succeeds.
  3. Pending work is determined by canonical source-context receipts.
  4. Clears a stale lock (crashed compile, killed process) by process liveness, never by age.

Usage:
    uv run python scripts/maybe_compile.py           # spawn if needed
    uv run python scripts/maybe_compile.py --force   # always spawn
    uv run python scripts/maybe_compile.py --status  # show lock state
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_liveness  # noqa: E402
from compile_lock import (
    claim_lock,
    clear_lock,
    is_compile_running,
    lock_owner_token,
    lock_state,
    process_status,
    write_lock,
)
from llm_wiki import runtime_roots
from memory_state import closed_daily_logs, daily_logs, spawn_detached
from memory_state import (
    file_hash as file_hash,
)
from memory_state import (
    load_state as load_state,
)


def compile_script() -> Path:
    return runtime_roots.vault_root() / "scripts" / "compile_memory.py"


def log_out_path() -> Path:
    return runtime_roots.state_root() / "logs" / "maybe-compile-last.log"


def log_err_path() -> Path:
    return runtime_roots.state_root() / "logs" / "maybe-compile-last.err.log"


def _is_pid_alive(pid: int) -> bool:
    """Cross-platform 'is this PID still running?' check; PID 0 is the placeholder."""
    if pid == 0:
        return True
    return process_liveness.pid_alive(pid)


def has_pending_work(closed_days_only: bool = False, *, deadline: float = float("inf")) -> bool:
    """A mirror is diagnostic; only context-bound receipt authority can skip work."""
    _trigger_active(deadline)
    paths = _candidate_logs(closed_days_only)
    _trigger_active(deadline)
    if not paths:
        return False
    if not (runtime_roots.vault_root() / "knowledge/daily/receipts").is_dir():
        return True
    return _unreceipted_daily_exists(paths, deadline)


def _trigger_active(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("compile trigger deadline exceeded")


def _unreceipted_daily_exists(paths: list[Path], deadline: float) -> bool:
    import compile_receipts
    from markdown_transaction import active_or_legacy_coordinator

    coordinator = active_or_legacy_coordinator(runtime_roots.vault_root(), runtime_roots.state_root(), deadline=deadline)
    selection = compile_receipts.receipt_predicate(coordinator, deadline=deadline)
    return any(not daily_receipted(path, selection) for path in paths)


def daily_receipted(path: Path, selection) -> bool:
    import compile_receipts

    selection._active()
    with compile_receipts.open_daily_source(path, deadline=selection.deadline) as content:
        return compile_receipts.daily_is_compiled(path.relative_to(runtime_roots.vault_root()).as_posix(), content, selection)


def _candidate_logs(closed_days_only: bool) -> list[Path]:
    daily_dir = runtime_roots.vault_root() / "knowledge" / "daily"
    if closed_days_only:
        return closed_daily_logs(daily_dir)
    return daily_logs(daily_dir)


def spawn_compile_if_idle(
    force: bool = False, closed_days_only: bool = False, *, deadline: float = float("inf")
) -> tuple[bool, str]:
    """Spawn a detached compile unless one is running or nothing is pending.

    Returns (spawned, reason). Admission refusals and expired deadlines remain
    visible to the caller. ``force`` bypasses the
    "no pending work" gate but never steals a live lock. ``closed_days_only``
    is the session start's: the day still being appended to waits for the
    nightly (`memory_state.closed_daily_logs`).
    """
    spawned, _skipped, reason = _spawn_outcome(force, closed_days_only, deadline=deadline)
    return (spawned, reason)


def _spawn_outcome(force: bool, closed_days_only: bool = False, *, deadline: float = float("inf")) -> tuple[bool, bool, str]:
    """(spawned, skipped, reason): a skip is a named refusal, not a failure."""
    _trigger_active(deadline)
    refusal = _refusal_before_claim(force, closed_days_only, deadline=deadline)
    if refusal is not None:
        return (False, True, refusal)
    _trigger_active(deadline)
    if not claim_lock():
        # Another caller took the lock between our check and our claim; name
        # what holds it now rather than a race the reader cannot verify.
        _state, reason = lock_state()
        return (False, True, f"skipped: {reason}")
    return _spawn_claimed(closed_days_only)


def _refusal_before_claim(force: bool, closed_days_only: bool = False, *, deadline: float = float("inf")) -> str | None:
    _trigger_active(deadline)
    is_running, reason = is_compile_running()
    if is_running:
        return _live_lock_refusal(force, reason)
    if not force and not has_pending_work(closed_days_only, deadline=deadline):
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


def compile_command(placeholder: str, closed_days_only: bool) -> list[str]:
    command = [
        sys.executable,
        str(compile_script()),
        "--trigger",
        "auto",
        "--lock-token",
        placeholder,
    ]
    if closed_days_only:
        command.append("--closed-days-only")
    return command


def _spawn_claimed(closed_days_only: bool = False) -> tuple[bool, bool, str]:
    # The placeholder's token is ours to clear if the spawn fails, and the
    # child's proof that the lock it finds was written for it — whether it
    # looks before or after the PID below is replaced.
    placeholder = lock_owner_token() or ""
    pid = spawn_detached(
        compile_command(placeholder, closed_days_only),
        stdout_path=log_out_path(),
        stderr_path=log_err_path(),
    )
    if pid is None:
        clear_lock(placeholder)
        return (False, False, "spawn failed")
    # Replace the placeholder PID (0) with the real one; the child owns it now.
    write_lock(pid, token=placeholder)
    return (True, False, f"spawned compile pid={pid}")


def status() -> dict:
    """Full snapshot for the metacognitive block, including pending work."""
    snapshot = process_status()
    snapshot["pending_work"] = False
    if not snapshot["compile_running"]:
        snapshot["pending_work"] = has_pending_work()
    return snapshot


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
