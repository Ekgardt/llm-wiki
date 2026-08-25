"""Shared helpers for memory automation state.

Three-zone layout: vault holds code + knowledge + gitignored runtime dirs.

    <vault>/
      run/state.json     # compile hashes, dedupe, heartbeats
      run/compile.pid    # maybe_compile lock
      run/queue/         # deferred LLM tasks
      logs/              # lint / nightly reports
      cache/             # FTS5/vector/graph indexes
                       # cache/cognee/ — optional semantic graph

`cache/` (incl. `cache/cognee/`), `logs/`, `run/` are gitignored — they live inside the
vault for single-checkout portability but git never tracks their churn.
Override the root via LLM_WIKI_STATE_ROOT (tests use a temp dir).

Written by multiple concurrent processes (flush_memory and compile_memory
may run at the same time). All writers MUST go through `update_state(mutator)`
so the mutation is applied on top of the latest on-disk version under a
cross-platform file lock — otherwise a slow writer will clobber fields
written by a faster one.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from reliable_memory import durable_publish_file, fsync_directory, sha256_bytes


def _resolve_vault_root(start: Path) -> Path:
    """Resolve the canonical vault root even from inside a git worktree.

    A naive `start.parent.parent` points to the worktree's own root, not
    the main vault. Git exposes the main repo via
    `git rev-parse --git-common-dir`, whose parent is the canonical vault.
    Falls back to the simple behavior if git is unavailable.
    """
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(start),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        git_common_dir = Path(out) if Path(out).is_absolute() else (start / out).resolve()
        git_common_dir = git_common_dir.resolve()
        if git_common_dir.name == ".git":
            return git_common_dir.parent
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass
    return start


# Canonical vault root: prefer LLM_WIKI_ROOT when set (installed instance),
# else resolve from this file's location (worktree-aware).
def _vault_root() -> Path:
    env = os.environ.get("LLM_WIKI_ROOT")
    if env:
        return Path(env).resolve()
    return _resolve_vault_root(Path(__file__).resolve().parent.parent)


ROOT = _vault_root()

# Runtime state lives INSIDE the vault as gitignored dirs (cache/, logs/,
# run/) — keeps everything in one checkout, git ignores the churn.
# Overridable via LLM_WIKI_STATE_ROOT for explicit portability (tests use a
# temp dir; multi-disk setups can point elsewhere).
STATE_ROOT = Path(
    os.environ.get("LLM_WIKI_STATE_ROOT", str(ROOT))
).resolve()
STATE_DIR = STATE_ROOT / "run"
REPORTS_DIR = STATE_ROOT / "logs"
CODE_TOOLS_DIR = STATE_ROOT / "cache/code-tools"
LSP_RUN_DIR = STATE_ROOT / "run/lsp"
STATE_FILE = STATE_DIR / "state.json"
LOCK_FILE = STATE_DIR / "state.json.lock"

# If a lock file is older than this, assume the holder died and steal it.
_STALE_LOCK_SECONDS = 30.0


def _windows_pid_alive(pid: int) -> bool:
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


def _posix_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError, OverflowError, ValueError):
        return False


def _is_pid_alive(pid: int) -> bool:
    """Cross-platform 'is this PID still running?' check.

    Same pattern as maybe_compile.py — used to decide whether a stale
    lock file belongs to a process that is genuinely dead (steal it)
    or merely slow (wait longer).
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_pid_alive(pid)
    return _posix_pid_alive(pid)


def load_state() -> dict[str, Any]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Preserve corrupt file for forensics; do not silently clobber.
        try:
            bak = STATE_FILE.with_suffix(".json.corrupt")
            bak.write_bytes(STATE_FILE.read_bytes())
            err_log = REPORTS_DIR / "hook-errors.log"
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            with err_log.open("a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] state.json corrupt; backed up to {bak.name}\n")
        except OSError:
            pass
        return {}


# What a reader of `run/state.json` is willing to read. `doctor` refuses a state
# file over 256 KiB and then reports that it could not check the scheduler or
# the captures at all — measured 2026-08-25, when the file reached 257 KiB and
# two health checks went blind. Writers keep it under three quarters of that, so
# growth shows up as eviction rather than as a blind spot.
MAX_STATE_TARGET_BYTES = 192 * 1024

# The maps that grow with use: dedupe memory and per-project reducers. Each is
# already capped by entry count, but an entry is not a fixed size, so the count
# caps alone never bounded the file.
_TRIMMABLE_STATE_KEYS = (
    "tool_capture_dedupe",
    "prompt_capture_dedupe",
    "project_checkpoint_reducers",
)


def _state_bytes(state: dict[str, Any]) -> int:
    return len(json.dumps(state, indent=2, ensure_ascii=False).encode("utf-8"))


def _evict_oldest(state: dict[str, Any], key: str) -> bool:
    """Drop the oldest entry of one growth map; True when something went."""
    entries = state.get(key)
    if not isinstance(entries, dict) or not entries:
        return False
    entries.pop(next(iter(entries)))
    return True


def trim_state_to_budget(state: dict[str, Any]) -> int:
    """Evict oldest dedupe and reducer entries until the file fits its bound.

    Returns how many entries were dropped. Losing a dedupe entry can cost one
    duplicate capture later; losing the health of two checks costs every finding
    they would have made, which is the worse of the two.
    """
    dropped = 0
    while _state_bytes(state) > MAX_STATE_TARGET_BYTES:
        if not any(_evict_oldest(state, key) for key in _TRIMMABLE_STATE_KEYS):
            return dropped
        dropped += 1
    return dropped


def save_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    trim_state_to_budget(state)
    atomic_write(STATE_FILE, json.dumps(state, indent=2, ensure_ascii=False))


def _sharing_violation(exc: PermissionError) -> bool:
    """Windows reports a held lock as a sharing violation, not as EEXIST."""
    if getattr(exc, "winerror", None) in {32, 33}:
        return True
    return sys.platform == "win32" and exc.errno == 13


def _lock_already_held() -> bool:
    return sys.platform == "win32" and LOCK_FILE.exists()


def _contention(exc: PermissionError, observed_contention: bool) -> bool:
    """A held lock is contention; an ACL denial is a real error."""
    if _sharing_violation(exc) or _lock_already_held():
        return True
    return observed_contention


def _claim_lock(owner_pid: str) -> int | None:
    """The lock descriptor when it was ours to take, None while contended."""
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except FileExistsError:
        return None
    os.write(fd, owner_pid.encode("utf-8"))
    return fd


def _lock_age() -> float:
    try:
        return time.time() - LOCK_FILE.stat().st_mtime
    except OSError:
        return 0.0


def _lock_owner_alive() -> bool:
    """True when the recorded owner is a live process; corrupt reads say no."""
    try:
        return _is_pid_alive(int(LOCK_FILE.read_text(encoding="utf-8").strip()))
    except (ValueError, OSError):
        return False


def _unlink_quietly() -> None:
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


def _wait_for_slow_owner(deadline: float, poll: float) -> None:
    """Sleep for the live owner, but never past the caller's deadline."""
    remaining = deadline - time.time()
    if remaining <= 0:
        raise TimeoutError(f"Could not acquire state lock: {LOCK_FILE}")
    time.sleep(min(poll * 10, remaining))


def _await_lock_turn(deadline: float, poll: float) -> None:
    """One turn of waiting: steal a dead lock, wait out a live one."""
    if _lock_age() > _STALE_LOCK_SECONDS:
        if _lock_owner_alive():
            _wait_for_slow_owner(deadline, poll)
            return
        _unlink_quietly()
        return
    if time.time() > deadline:
        raise TimeoutError(f"Could not acquire state lock: {LOCK_FILE}")
    time.sleep(poll)


def _acquire_state_lock(owner_pid: str, deadline: float, poll: float) -> int:
    observed_contention = False
    while True:
        try:
            fd = _claim_lock(owner_pid)
        except PermissionError as exc:
            if not _contention(exc, observed_contention):
                raise
            fd = None
        if fd is not None:
            return fd
        observed_contention = True
        _await_lock_turn(deadline, poll)


def _release_state_lock(fd: int, owner_pid: str) -> None:
    """Close the descriptor and unlink only while the lock is still ours.

    A stale-lock thief may have deleted our lock and another process may hold a
    fresh one; deleting theirs would hand the state file to two writers.
    """
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        if LOCK_FILE.read_text(encoding="utf-8").strip() == owner_pid:
            LOCK_FILE.unlink()
    except OSError:
        pass


@contextmanager
def _state_lock(timeout: float = 10.0, poll: float = 0.05) -> Iterator[None]:
    """Cross-platform advisory lock via O_CREAT|O_EXCL on a sidecar file.

    Works on Windows and POSIX without extra deps. If the lock file is stale
    (older than `_STALE_LOCK_SECONDS`) and its owner is gone, we steal it; if
    the owner is alive but slow, we wait instead of killing its write.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    owner_pid = str(os.getpid())
    fd = _acquire_state_lock(owner_pid, time.time() + timeout, poll)
    try:
        yield
    finally:
        _release_state_lock(fd, owner_pid)


def update_state(
    mutator: Callable[[dict[str, Any]], None], *, lock_timeout: float = 10.0
) -> dict[str, Any]:
    """Atomically read-modify-write state under a file lock.

    `mutator` receives the freshly-loaded state dict and mutates it
    in place. The updated dict is written back atomically. Returns the
    state that was written, so callers can inspect the post-merge result.
    `lock_timeout` bounds lock acquisition while preserving the default
    timeout for scheduled and other non-hook writers.
    """
    with _state_lock(timeout=lock_timeout):
        state = load_state()
        mutator(state)
        save_state(state)
        return state


def file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def atomic_write(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write content through the checked durable publication boundary."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = content.encode(encoding)
    staged = path.parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
    descriptor = os.open(
        staged,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        destination_size = path.lstat().st_size
    except FileNotFoundError:
        destination_size = 0
    outcome = durable_publish_file(
        staged,
        path,
        replace=True,
        expected_sha256=sha256_bytes(payload),
        max_bytes=max(1, len(payload), destination_size),
    )
    if outcome == "duplicate":
        staged.unlink()
        fsync_directory(path.parent)


def _stream_target(path: Path | None):
    """(handle, value) for one redirect: a file when asked, else DEVNULL."""
    if path is None:
        return None, subprocess.DEVNULL
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "wb")  # noqa: SIM115 - closed by the caller after spawn
    return handle, handle


def _detached_flags() -> dict[str, Any]:
    if sys.platform != "win32":
        return {"start_new_session": True}
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000
    return {
        "creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    }


def _detached_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["CLAUDE_INVOKED_BY"] = env.get("CLAUDE_INVOKED_BY", "memory-automation")
    return env


def _closed_quietly(handle) -> None:
    if handle is None:
        return
    try:
        handle.close()
    except OSError:
        pass


def _spawned_pid(args: list[str], kwargs: dict[str, Any]) -> int | None:
    try:
        return subprocess.Popen(args, **kwargs).pid
    except OSError:
        return None


def spawn_detached(
    args: list[str],
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> int | None:
    """Spawn a subprocess that outlives the caller.

    Used by hook wrappers to kick off flush/compile without blocking the
    hook timeout. Safe on Windows (DETACHED_PROCESS) and POSIX
    (start_new_session).

    If `stdout_path` / `stderr_path` are given, stdout/stderr are redirected
    there (truncated on each spawn) instead of DEVNULL — this is how we keep
    observability into a detached compile. Returns the spawned PID, or None if
    spawn failed.
    """
    out_handle, out_value = _stream_target(stdout_path)
    err_handle, err_value = _stream_target(stderr_path)
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
        "cwd": str(ROOT),
        "stdout": out_value,
        "stderr": err_value,
        "env": _detached_environment(),
        **_detached_flags(),
    }
    pid = _spawned_pid(args, kwargs)
    # The parent can close its handles; the child inherited its own.
    _closed_quietly(out_handle)
    _closed_quietly(err_handle)
    return pid
