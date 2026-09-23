"""Shared helpers for memory automation state.

Three-zone layout: vault holds code + knowledge + gitignored runtime dirs.

    <vault>/
      run/state.json     # compile hashes, dedupe, heartbeats
      run/compile.pid    # maybe_compile lock
      run/queue-v3.sqlite3   # deferred LLM tasks (run/queue/*.json: migration input only)
      logs/              # lint / nightly reports
      cache/             # FTS5/vector/graph indexes (cache/cognee/ is retired)

`cache/`, `logs/`, `run/` are gitignored — they live inside the
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
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import process_liveness
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
# One capture intent on disk; the hook that writes it and the worker that
# reads it bound the same file (`docs/research/2026-09-23-one-limit-one-place.md`).
MAX_CAPTURE_INTENT_BYTES = 1024 * 1024
# How long a hook waits for `state.json`: the host is waiting on the hook.
HOOK_STATE_LOCK_TIMEOUT = 0.1

# If a lock file is older than this, assume the holder died and steal it.
_STALE_LOCK_SECONDS = 30.0


def _is_pid_alive(pid: int) -> bool:
    """Cross-platform 'is this PID still running?' — one probe, doubt is alive.

    Used to decide whether a stale lock file belongs to a process that is
    genuinely dead (retire it) or merely slow or foreign (wait longer). A PID
    that is not a positive integer is never alive.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    return process_liveness.pid_alive(pid)


def _decoded_state(raw: bytes) -> dict[str, Any] | None:
    """The state these bytes hold, or None: not UTF-8, not JSON, or not an object.

    One definition of "readable" for the reader and the writer. See
    `docs/research/2026-09-17-a-torn-state-file-is-recovered-whatever-its-bytes.md`.
    """
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _keep_corrupt_copy(raw: bytes) -> None:
    """Preserve the corrupt bytes for forensics; do not silently clobber."""
    try:
        bak = STATE_FILE.with_suffix(".json.corrupt")
        bak.write_bytes(raw)
        err_log = REPORTS_DIR / "hook-errors.log"
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        with err_log.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] state.json corrupt; backed up to {bak.name}\n")
    except OSError:
        pass


def load_state() -> dict[str, Any]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        return {}
    raw = STATE_FILE.read_bytes()
    state = _decoded_state(raw)
    if state is None:
        _keep_corrupt_copy(raw)
        return {}
    return state


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


# Untranslated: what the payload says is what the lock file holds.
_LOCK_OPEN_FLAGS = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_BINARY", 0)


def _lock_already_held() -> bool:
    return sys.platform == "win32" and LOCK_FILE.exists()


def _contention(exc: PermissionError, observed_contention: bool) -> bool:
    """A held lock is contention; an ACL denial is a real error."""
    if _sharing_violation(exc) or _lock_already_held():
        return True
    return observed_contention


def _claim_lock(payload: bytes) -> int | None:
    """The lock descriptor when it was ours to take, None while contended.

    The descriptor is binary: a Windows text-mode descriptor would write the
    payload's newlines as CRLF, `_release_state_lock` would never recognise its
    own lock again, and the file would outlive every writer. Research:
    docs/research/2026-09-18-a-payload-is-written-as-the-bytes-it-is.md
    """
    try:
        fd = os.open(str(LOCK_FILE), _LOCK_OPEN_FLAGS)
    except FileExistsError:
        return None
    os.write(fd, payload)
    return fd


def _own_process_identity() -> str:
    """This process's start identity, or empty when the probe cannot settle it."""
    try:
        return process_liveness.process_start_identity(os.getpid()) or ""
    except (OSError, ValueError):
        return ""


def _lock_payload() -> bytes:
    """Who holds the lock: the PID, and the identity that outlives PID reuse.

    A PID alone cannot say whether its owner died and its number was handed to
    another process. Research:
    docs/research/2026-09-17-a-lock-names-the-process-not-only-its-number.md
    """
    return f"{os.getpid()}\n{_own_process_identity()}\n".encode()


def _lock_age() -> float:
    try:
        return time.time() - LOCK_FILE.stat().st_mtime
    except OSError:
        return 0.0


def _lock_bytes() -> bytes | None:
    try:
        return LOCK_FILE.read_bytes()
    except OSError:
        return None


def _lock_owner(payload: bytes | None) -> tuple[int, str] | None:
    """(PID, start identity) the lock records; None when it records neither.

    The identity line is absent in a lock written before this release, and the
    answer for such a file is the PID probe, exactly as it was.
    """
    if not payload:
        return None
    return _owner_from_lines(payload.decode("utf-8", errors="replace").splitlines())


def _owner_from_lines(lines: list[str]) -> tuple[int, str] | None:
    if not lines:
        return None
    try:
        pid = int(lines[0].strip())
    except ValueError:
        return None
    return (pid, lines[1].strip() if len(lines) > 1 else "")


def _owner_alive(payload: bytes | None) -> bool:
    """True when the recorded owner is a live process; corrupt reads say no."""
    owner = _lock_owner(payload)
    if owner is None:
        return False
    return process_liveness.owner_alive(*owner)


def _named_owner_is_dead(payload: bytes | None) -> bool:
    """A finished lock file that names a provably dead owner — no age needed.

    A writer that died holding the lock used to stop every other writer for the
    30 s staleness window, while their own wait is 10 s (audit Q-L6). A lock
    ending in its newline is a finished write, so what it names can be judged.
    """
    if not payload or not payload.endswith(b"\n"):
        return False
    owner = _lock_owner(payload)
    if owner is None or not owner[1]:
        return False
    return not process_liveness.owner_alive(*owner)


# How long a stealer waits for another stealer to finish judging the same lock.
STEAL_GUARD_SECONDS = 5.0


def _lock_descriptor_exclusively(descriptor: int) -> None:
    """One non-blocking exclusive OS lock attempt; the kernel drops it if we die."""
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _await_guard(descriptor: int, guard: Path) -> None:
    deadline = time.monotonic() + STEAL_GUARD_SECONDS
    while True:
        try:
            _lock_descriptor_exclusively(descriptor)
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise StateLockTimeout(f"Could not acquire steal guard: {guard}") from exc
            time.sleep(0.01)


@contextmanager
def _steal_guard(path: Path) -> Iterator[None]:
    """One stealer at a time for this lock: check and removal under one OS lock.

    See `docs/research/2026-09-14-one-stealer-at-a-time.md`.
    """
    guard = path.with_name(f"{path.name}.steal")
    descriptor = os.open(str(guard), os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
    try:
        _await_guard(descriptor, guard)
        yield
    finally:
        os.close(descriptor)


def _current_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def retire_stale_lock(path: Path, judged: bytes) -> bool:
    """Remove `path` only while it still holds the bytes the caller judged stale.

    Under the steal guard the bytes are checked before anything moves: while the judged
    file exists no creator can make a lock (`O_EXCL`) and its dead owner cannot release
    it, so the file checked is the file removed. Research:
    docs/research/2026-09-10-a-stale-lock-is-moved-aside-and-checked-before-it-is-removed.md,
    docs/research/2026-09-14-one-stealer-at-a-time.md
    """
    with _steal_guard(path):
        if _current_bytes(path) != judged:
            return False
        path.unlink(missing_ok=True)
        return True


class StateLockTimeout(TimeoutError):
    """The state lock is held by a live writer; the caller's event is retried."""


class StateCorrupt(OSError):
    """`run/state.json` and its previous version are both unreadable; nothing was written."""


def _wait_for_slow_owner(deadline: float, poll: float) -> None:
    """Sleep for the live owner, but never past the caller's deadline."""
    remaining = deadline - time.time()
    if remaining <= 0:
        raise StateLockTimeout(f"Could not acquire state lock: {LOCK_FILE}")
    time.sleep(min(poll * 10, remaining))


def _await_lock_turn(deadline: float, poll: float) -> None:
    """One turn of waiting: retire a dead lock, wait out a live one."""
    payload = _lock_bytes()
    if _named_owner_is_dead(payload):
        retire_stale_lock(LOCK_FILE, payload)
        return
    _judge_by_age(payload, deadline, poll)


def _judge_by_age(payload: bytes | None, deadline: float, poll: float) -> None:
    """The rule for a lock that does not name its owner: wait, then judge by age."""
    if _lock_age() > _STALE_LOCK_SECONDS:
        _retire_or_wait(payload, deadline, poll)
        return
    if time.time() > deadline:
        raise StateLockTimeout(f"Could not acquire state lock: {LOCK_FILE}")
    time.sleep(poll)


def _retire_or_wait(payload: bytes | None, deadline: float, poll: float) -> None:
    if _owner_alive(payload):
        _wait_for_slow_owner(deadline, poll)
        return
    if payload is not None:
        retire_stale_lock(LOCK_FILE, payload)


def _acquire_state_lock(payload: bytes, deadline: float, poll: float) -> int:
    observed_contention = False
    while True:
        try:
            fd = _claim_lock(payload)
        except PermissionError as exc:
            if not _contention(exc, observed_contention):
                raise
            fd = None
        if fd is not None:
            return fd
        observed_contention = True
        _await_lock_turn(deadline, poll)


# Windows will not delete a file somebody else has open. Microsoft: "The
# DeleteFile function fails if an application attempts to delete a file that has
# other handles open for normal I/O ... (FILE_SHARE_DELETE must have been
# specified when other handles were opened)", and Python's open() does not ask
# for it. Every waiter polls this lock through `_lock_bytes`, so a holder's
# unlink lands inside a reader's handle often enough to matter — and a release
# that gives up leaves the lock naming an owner that is alive, which no
# staleness rule ever retires. These are the three errors `lsp_process` already
# retries for its lease. Research:
# docs/research/2026-09-18-a-lock-is-released-even-while-somebody-is-reading-it.md
_WINDOWS_SHARING_ERRORS = frozenset({5, 32, 33})

# A reader holds the lock file for microseconds, so this is a margin of about a
# million against the window it races, and still well inside the lock timeout.
LOCK_RELEASE_SECONDS = 2.0


def _reader_blocked_unlink(exc: OSError) -> bool:
    """Whether a reader is merely holding the file open for an instant."""
    return getattr(exc, "winerror", None) in _WINDOWS_SHARING_ERRORS


def _try_unlink_lock_file() -> bool | None:
    """True when the lock is gone, False on a real error, None to try again."""
    try:
        os.unlink(LOCK_FILE)
    except FileNotFoundError:
        return True
    except OSError as exc:
        return None if _reader_blocked_unlink(exc) else False
    return True


def _unlink_lock_file() -> bool:
    """Remove our lock, waiting out the readers Windows lets block a delete."""
    deadline = time.monotonic() + LOCK_RELEASE_SECONDS
    while True:
        outcome = _try_unlink_lock_file()
        if outcome is not None:
            return outcome
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)


def _release_state_lock(fd: int, payload: bytes) -> None:
    """Close the descriptor and unlink only while the lock is still ours.

    A stale-lock thief may have deleted our lock and another process may hold a
    fresh one; deleting theirs would hand the state file to two writers.
    """
    try:
        os.close(fd)
    except OSError:
        pass
    if _lock_bytes() == payload:
        _unlink_lock_file()


@contextmanager
def _state_lock(timeout: float = 10.0, poll: float = 0.05) -> Iterator[None]:
    """Cross-platform advisory lock via O_CREAT|O_EXCL on a sidecar file.

    Works on Windows and POSIX without extra deps. If the lock file is stale
    (older than `_STALE_LOCK_SECONDS`) and its owner is gone, we steal it; if
    the owner is alive but slow, we wait instead of killing its write.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = _lock_payload()
    fd = _acquire_state_lock(payload, time.time() + timeout, poll)
    try:
        yield
    finally:
        _release_state_lock(fd, payload)


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
        state, readable = _state_for_update()
        mutator(state)
        _keep_previous(readable)
        save_state(state)
        return state


# A writer never turns an unreadable state into an empty one: it recovers the
# previous version or writes nothing. See
# `docs/research/2026-09-14-a-corrupt-state-is-not-replaced-by-an-empty-one.md`.
def _previous_state_file() -> Path:
    return STATE_FILE.with_name(f"{STATE_FILE.name}.previous")


def _parsed_state(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return _decoded_state(raw)


def _state_for_update() -> tuple[dict[str, Any], bool]:
    """(the state to mutate, whether the current file was readable)."""
    if not STATE_FILE.exists():
        return {}, False
    current = _parsed_state(STATE_FILE)
    if current is not None:
        return current, True
    return _recovered_previous(), False


def _recovered_previous() -> dict[str, Any]:
    """The previous version of an unreadable state file, or `StateCorrupt`."""
    load_state()  # keeps the forensic copy and logs the corruption
    previous = _parsed_state(_previous_state_file())
    if previous is None:
        raise StateCorrupt(f"state file unreadable and no readable previous version: {STATE_FILE}")
    return previous


def _keep_previous(readable: bool) -> None:
    """Hard-link the readable file about to be replaced to its `.previous` name."""
    if not readable:
        return
    staged = STATE_FILE.with_name(f".{STATE_FILE.name}.previous.{secrets.token_hex(8)}.tmp")
    try:
        os.link(STATE_FILE, staged)
        os.replace(staged, _previous_state_file())
    except OSError:
        staged.unlink(missing_ok=True)


# `knowledge/daily/` also holds a tracked `README.md`. It is not a daily log:
# it has no date and is never compiled. Every reader of that directory asks
# here, so the rule has one home and no reader can forget it.
# Research: docs/research/2026-09-17-a-daily-log-is-named-by-its-date-everywhere.md
DAILY_LOG_NAME = re.compile(r"\d{4}-\d{2}-\d{2}\.md")


def daily_logs(daily_dir: Path) -> list[Path]:
    """The `YYYY-MM-DD.md` files of a daily directory, oldest first; none if it is absent."""
    if not daily_dir.is_dir():
        return []
    return sorted(
        path for path in daily_dir.glob("*.md") if DAILY_LOG_NAME.fullmatch(path.name) is not None
    )


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
    # A failed publication deliberately leaves the staged file: it is a
    # complete, fsynced copy of what was to be written, and the test above this
    # behaviour calls it recoverable staging. What was missing is that nothing
    # ever collected one — 39 orphans weighing 272 MB by 2026-08-30, the oldest
    # four days old. `reclaim_runtime_state.py` sweeps them once they are an
    # hour old, which no live write can be.
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
