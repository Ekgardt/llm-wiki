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

import hashlib
import io
import json
import os
import sys
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

from markdown_transaction import append_knowledge, stable_operation_id  # noqa: E402
from secret_redact import redact_secrets  # noqa: E402

# How long a hook's append may try before it gives up and says why. Each is
# shorter than the host's own timeout for that hook, with room left to start
# the interpreter and to write the failure line: the shipped prompt and tool
# hooks get 5 seconds, the session-end hook 15 (its delegate 10). A writer with
# no deadline retried until it was killed and left no reason. See
# `docs/research/2026-09-17-every-hook-writer-gives-up-before-its-host-does.md`.
BREADCRUMB_APPEND_BUDGET_SECONDS = 3.0
LIFECYCLE_APPEND_BUDGET_SECONDS = 7.0


def append_deadline(budget_seconds: float) -> float:
    """The monotonic instant a hook's append stops trying."""
    return time.monotonic() + budget_seconds


def locked_append(
    daily_path: Path,
    text: str,
    operation_id: str | None = None,
    *,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Append text to a daily-log file under the shared cross-process lock.

    This is the lowest-level locked writer. Higher-level functions like
    ``append_daily()`` and external callers (``flush_memory``,
    ``session_end_project_tag``) both delegate here so that all daily-log
    writes share a single serialization point.
    """
    text = redact_secrets(text)
    header = f"# Daily Session Memory — {daily_path.stem}\n".encode()
    if not daily_path.exists():
        append_knowledge(
            stable_operation_id("daily-header", daily_path.name, header),
            daily_path,
            header,
            deadline=deadline,
            cancelled=cancelled,
        )
    block = (text if text.endswith("\n") else text + "\n").encode("utf-8")
    append_knowledge(
        operation_id,
        daily_path,
        block,
        deadline=deadline,
        cancelled=cancelled,
    )


def locked_append_once(
    daily_path: Path,
    text: str,
    operation_id: str,
    *,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    """Append one operation exactly once; the transaction's append serializes writers."""
    marker = _unappended_marker(daily_path, operation_id)
    if marker is None:
        return False
    header = f"# Daily Session Memory — {daily_path.stem}\n".encode()
    if not daily_path.exists():
        append_knowledge(
            stable_operation_id("daily-header", daily_path.name, header),
            daily_path,
            header,
            deadline=deadline,
            cancelled=cancelled,
        )
    block = redact_secrets(
        f"\n{marker}\n{text}{'' if text.endswith(chr(10)) else chr(10)}"
    ).encode("utf-8")
    append_knowledge(
        operation_id,
        daily_path,
        block,
        deadline=deadline,
        cancelled=cancelled,
    )
    return True


def _unappended_marker(daily_path: Path, operation_id: str) -> str | None:
    """The operation's marker comment; None when the daily log already carries it."""
    if not operation_id:
        raise ValueError("operation_id must be non-empty")
    marker_id = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    marker = f"<!-- llm-wiki-operation:{marker_id} -->"
    if any(_carries(path, marker) for path in _logs_that_may_hold(daily_path)):
        return None
    return marker


def _logs_that_may_hold(daily_path: Path) -> list[Path]:
    """Today's log and the day before: a redelivery lands within one midnight of the first write.

    See `docs/research/2026-09-14-a-redelivery-after-midnight-is-recognised.md`.
    """
    try:
        day = date.fromisoformat(daily_path.stem)
    except ValueError:
        return [daily_path]
    return [daily_path, daily_path.with_name(f"{day - timedelta(days=1)}.md")]


def _carries(path: Path, marker: str) -> bool:
    return path.exists() and marker in path.read_text(encoding="utf-8")


def append_daily(
    slug: str,
    session_id: str,
    block: str,
    operation_id: str | None = None,
    *,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Append a pre-built block to today's daily log (unified locked writer).

    This is the SINGLE entry point all daily-log writers must use. Every
    write goes through the transaction's ``append_knowledge``, which
    serializes concurrent hooks (UserPromptSubmit, PostToolUse,
    flush_memory) across processes, so their blocks never interleave.

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
    if operation_id:
        locked_append_once(
            path,
            text,
            operation_id,
            deadline=deadline,
            cancelled=cancelled,
        )
    else:
        locked_append(
            path,
            text,
            deadline=deadline,
            cancelled=cancelled,
        )
    return path


def main() -> int:
    payload = _stdin_payload()
    if payload is None:
        return 0
    block = payload.get("block") or ""
    if not block:
        return 0
    _append_event(payload, redact_secrets(block))
    return 0


def _stdin_payload() -> dict | None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _append_event(payload: dict, block: str) -> None:
    try:
        append_daily(
            payload.get("slug", ""),
            payload.get("sessionId", ""),
            block,
            operation_id=_event_operation_id(payload),
        )
    except Exception as error:  # noqa: BLE001 - the helper's contract is exit 0; the reason is kept
        report_helper_failure(
            "daily_log_append", "opencode_daily_append", error, payload.get("sessionId")
        )


def report_helper_failure(
    helper: str, kind: str, error: BaseException, session_id: object
) -> None:
    """The reason on stderr and in the capture-failure trail; never an exit status.

    The plugin helpers say "never fails" and caught `OSError` only, while the
    writer also raises value, transaction and ownership errors. See
    `docs/research/2026-09-17-a-helper-that-says-it-never-fails-does-not.md`.
    """
    from capture_diagnostics import record_capture_failure

    reason = redact_secrets(f"{type(error).__name__}: {error}")
    print(f"{helper}: write failed: {reason}", file=sys.stderr)
    known_session = session_id if isinstance(session_id, str) else None
    record_capture_failure(kind, reason, error=error, session_id=known_session)


def _event_operation_id(payload: dict) -> str | None:
    event_id = payload.get("eventId") or payload.get("operationId")
    if isinstance(event_id, str) and event_id:
        return f"daily-event:{event_id}"
    return None


if __name__ == "__main__":
    raise SystemExit(main())
