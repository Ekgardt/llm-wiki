"""Helper for OpenCode plugin: append a pre-built block to today's daily log.

Reads JSON from stdin: {"slug": "...", "sessionId": "...", "block": "..."}
Appends `block` to $LLM_WIKI_ROOT/knowledge/daily/<date>.md.

Why this exists: the OpenCode plugin does LLM work in JS (via OpenCode SDK),
then needs to write the result to a markdown file. Calling Python for the
file I/O keeps path handling cross-platform and reuses the canonical
daily-log location without re-implementing it in JS.

Never fails — always exits 0. Errors go to stderr.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

from memory_state import STATE_ROOT, _is_pid_alive  # noqa: E402
from secret_redact import redact_secrets  # noqa: E402


@contextlib.contextmanager
def _daily_lock(timeout: float = 10.0, poll: float = 0.05):
    """Cross-platform advisory lock via O_CREAT|O_EXCL on a sidecar file.

    Same atomic pattern as memory_state._state_lock. Works on Windows AND
    POSIX. Fail-closed: raises TimeoutError if lock can't be acquired.

    Includes stale-lock recovery: if the lock file is older than 30s,
    checks if the owner PID is alive. If dead, steals the lock. If alive,
    waits. This prevents perpetual lockout if a holder crashes.
    """
    lock_file = STATE_ROOT / "run" / "daily-append.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    owner_pid = str(os.getpid())
    fd: int | None = None
    while True:
        try:
            fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, owner_pid.encode("utf-8"))
            break
        except FileExistsError:
            try:
                age = time.time() - lock_file.stat().st_mtime
            except OSError:
                age = 0.0
            if age > 30.0:
                # Check if owner is alive before stealing
                try:
                    prev_pid = int(lock_file.read_text(encoding="utf-8").strip())
                    alive = _is_pid_alive(prev_pid)
                    if alive:
                        if time.time() > deadline:
                            raise TimeoutError(
                                f"Could not acquire daily-log lock: {lock_file}"
                            )
                        time.sleep(poll)
                        continue
                except TimeoutError:
                    raise
                except (ValueError, OSError):
                    pass
                try:
                    lock_file.unlink()
                except OSError:
                    pass
                if time.time() > deadline:
                    raise TimeoutError(
                        f"Could not acquire daily-log lock: {lock_file}"
                    )
                continue
            if time.time() > deadline:
                raise TimeoutError(f"Could not acquire daily-log lock: {lock_file}")
            time.sleep(poll)
    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        # Owner-aware deletion
        try:
            current = lock_file.read_text(encoding="utf-8").strip()
            if current == owner_pid:
                lock_file.unlink()
        except OSError:
            pass


def locked_append(daily_path: Path, text: str) -> None:
    """Append text to a daily-log file under the shared cross-process lock.

    This is the lowest-level locked writer. Higher-level functions like
    ``append_daily()`` and external callers (``flush_memory``,
    ``session_end_project_tag``) both delegate here so that all daily-log
    writes share a single serialization point.
    """
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    with _daily_lock():
        if not daily_path.exists():
            day = daily_path.stem
            daily_path.write_text(f"# Daily Session Memory — {day}\n", encoding="utf-8")
        with daily_path.open("a", encoding="utf-8") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")


def append_daily(slug: str, session_id: str, block: str) -> Path:
    """Append a pre-built block to today's daily log (unified locked writer).

    This is the SINGLE entry point all daily-log writers must use. It
    acquires the cross-process ``_daily_lock()`` so concurrent hooks
    (UserPromptSubmit, PostToolUse, flush_memory) cannot interleave
    their writes and corrupt the daily file.

    Args:
        slug: Project slug (for context — included in the block by caller).
        session_id: Session identifier (for context — included by caller).
        block: The pre-formatted markdown block to append.

    Returns:
        Path to the daily log file that was written.
    """
    root = Path(
        os.environ.get("LLM_WIKI_ROOT", str(Path(__file__).resolve().parent.parent))
    ).resolve()
    daily_dir = root / "knowledge" / "daily"
    day = datetime.now().strftime("%Y-%m-%d")
    path = daily_dir / f"{day}.md"
    text = "\n" + block if not block.startswith("\n") else block
    locked_append(path, text)
    return path


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return 0

    if not isinstance(payload, dict):
        return 0

    block = payload.get("block") or ""
    if not block:
        return 0
    block = redact_secrets(block)

    try:
        append_daily(payload.get("slug", ""), payload.get("sessionId", ""), block)
    except OSError as e:
        print(f"daily_log_append: write failed: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
