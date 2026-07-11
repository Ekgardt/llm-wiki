"""Shared helpers for memory automation state.

Three-zone layout: vault holds code + knowledge + gitignored runtime dirs.

    <vault>/
      run/state.json     # compile hashes, dedupe, heartbeats
      run/compile.pid    # maybe_compile lock
      run/queue/         # deferred LLM tasks
      logs/              # lint / nightly reports
      cache/             # search / QMD indexes
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
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


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
STATE_FILE = STATE_DIR / "state.json"
LOCK_FILE = STATE_DIR / "state.json.lock"

# If a lock file is older than this, assume the holder died and steal it.
_STALE_LOCK_SECONDS = 30.0


def _is_pid_alive(pid: int) -> bool:
    """Cross-platform 'is this PID still running?' check.

    Same pattern as maybe_compile.py — used to decide whether a stale
    lock file belongs to a process that is genuinely dead (steal it)
    or merely slow (wait longer).
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
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
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError, OverflowError, ValueError):
            return False


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


def save_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)


@contextmanager
def _state_lock(timeout: float = 10.0, poll: float = 0.05) -> Iterator[None]:
    """Cross-platform advisory lock via O_CREAT|O_EXCL on a sidecar file.

    Works on Windows and POSIX without extra deps. If the lock file is
    stale (older than _STALE_LOCK_SECONDS), we steal it.

    Owner-aware: writes the owner's PID to the lock file on acquisition.
    In the finally block, only deletes the lock if it still contains our
    PID — prevents a slow holder from deleting a fresh lock that a
    stale-lock thief's victim legitimately acquired. Before stealing a
    stale lock, checks whether the owner PID is still alive; if it is,
    waits rather than stealing (avoids killing a slow writer).
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    fd: int | None = None
    owner_pid = str(os.getpid())
    while True:
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, owner_pid.encode("utf-8"))
            break
        except FileExistsError:
            try:
                age = time.time() - LOCK_FILE.stat().st_mtime
            except OSError:
                age = 0.0
            if age > _STALE_LOCK_SECONDS:
                # Before stealing, check if owner is actually dead
                try:
                    lock_content = LOCK_FILE.read_text(encoding="utf-8")
                    prev_pid = int(lock_content.strip())
                    if _is_pid_alive(prev_pid):
                        # Owner is alive but slow — wait, but don't
                        # sleep past the original deadline.
                        remaining = deadline - time.time()
                        if remaining <= 0:
                            raise TimeoutError(
                                f"Could not acquire state lock: {LOCK_FILE}"
                            )
                        time.sleep(min(poll * 10, remaining))
                        continue
                except (ValueError, OSError):
                    pass  # Corrupt lock — steal it
                try:
                    LOCK_FILE.unlink()
                except OSError:
                    pass
                continue
            if time.time() > deadline:
                raise TimeoutError(f"Could not acquire state lock: {LOCK_FILE}")
            time.sleep(poll)
    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        # Owner-aware deletion: only unlink if the lock file still contains
        # our PID. If a stale-lock thief deleted our lock and another
        # process created a new one, we must NOT delete theirs.
        try:
            current = LOCK_FILE.read_text(encoding="utf-8").strip()
            if current == owner_pid:
                LOCK_FILE.unlink()
        except OSError:
            pass


def update_state(mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Atomically read-modify-write state under a file lock.

    `mutator` receives the freshly-loaded state dict and mutates it
    in place. The updated dict is written back atomically. Returns the
    state that was written, so callers can inspect the post-merge result.
    """
    with _state_lock():
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
    """Write content atomically via temp file + os.replace.

    Guarantees that readers never see a partial/truncated file: either
    the old version is intact or the new version is fully written.
    Used for all durable writes (notes, locks, index, cache files).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding=encoding)
    os.replace(str(tmp), str(path))


def spawn_detached(
    args: list[str],
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> int | None:
    """Spawn a subprocess that outlives the caller.

    Used by hook wrappers to kick off flush/compile without blocking the
    hook timeout. Safe on Windows (DETACHED_PROCESS) and POSIX (start_new_session).

    If `stdout_path` / `stderr_path` are given, stdout/stderr are redirected
    there (truncated on each spawn) instead of DEVNULL — this is how we
    keep observability into a detached compile. Returns the spawned PID,
    or None if spawn failed.
    """
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
        "cwd": str(ROOT),
    }
    out_f = err_f = None
    if stdout_path is not None:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        out_f = open(stdout_path, "wb")
        kwargs["stdout"] = out_f
    else:
        kwargs["stdout"] = subprocess.DEVNULL
    if stderr_path is not None:
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        err_f = open(stderr_path, "wb")
        kwargs["stderr"] = err_f
    else:
        kwargs["stderr"] = subprocess.DEVNULL
    env = os.environ.copy()
    env["CLAUDE_INVOKED_BY"] = env.get("CLAUDE_INVOKED_BY", "memory-automation")
    kwargs["env"] = env
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    pid: int | None = None
    try:
        proc = subprocess.Popen(args, **kwargs)
        pid = proc.pid
    except OSError:
        pid = None
    # Parent can close its handles; the child inherited its own.
    if out_f is not None:
        try:
            out_f.close()
        except OSError:
            pass
    if err_f is not None:
        try:
            err_f.close()
        except OSError:
            pass
    return pid
