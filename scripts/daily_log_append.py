"""Append to the daily log under its lock: the one writer every capture path shares.

`locked_append` and `locked_append_once` add a block to a day's log with an
operation marker, so a retry finds the block instead of writing it twice;
`append_daily` files a block under the day it belongs to. The capture hooks, the
queue worker, the MCP server and episode consolidation all write through here.

The module had a command line for an OpenCode plugin that stopped calling it on
2026-07-13; it was retired on 2026-09-25
(docs/research/2026-09-25-the-plugin-helpers-nothing-calls-are-retired.md).
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import sys
import time
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

from iso_time import local_now  # noqa: E402
from markdown_transaction import (  # noqa: E402
    _WRITER_LEASE_SECONDS,
    append_knowledge,
    stable_operation_id,
)
from secret_redact import redact_secrets  # noqa: E402

# How long a hook's append may try before it gives up and says why. Each is
# shorter than the host's own timeout for that hook, with room left to start
# the interpreter and to write the failure line: the shipped prompt and tool
# hooks get 5 seconds, the session-end hook 15 (its delegate 10). A writer with
# no deadline retried until it was killed and left no reason. See
# `docs/research/2026-09-17-every-hook-writer-gives-up-before-its-host-does.md`.
# 2.5 s plus 1 s to start the delegate stays under the host's 5 s with room for
# the adapter itself (audit 2026-09-26 C-1).
BREADCRUMB_APPEND_BUDGET_SECONDS = 2.5
LIFECYCLE_APPEND_BUDGET_SECONDS = 7.0
# A tool breadcrumb whose hook the host runs in the background (Claude Code
# `async: true`) has no host limit, so it waits out one full writer lease (the
# longest a live writer may hold the gate) and then gets one breadcrumb append.
# On 2026-09-28 the 2.5 s budget lost 738 breadcrumbs to gate holds and a busy
# CPU; see docs/research/2026-09-29-a-tool-breadcrumb-waits-in-the-background.md.
BACKGROUND_APPEND_BUDGET_SECONDS = _WRITER_LEASE_SECONDS + BREADCRUMB_APPEND_BUDGET_SECONDS


def append_deadline(budget_seconds: float) -> float:
    """The monotonic instant a hook's append stops trying."""
    return time.monotonic() + budget_seconds


# A line that opens a daily entry of its own: a `## [id]` heading or the operation
# marker (`evidence_resolver.daily_entries`). A block keeps its own first line;
# any later line of this shape came from a field the writer interpolated — a tool
# path, a prompt, a model's answer — and would forge an entry, so it is escaped.
# Every daily write passes here, so a new writer is covered without being listed
# (audit 2026-09-27 B-5, docs/research/2026-09-27-a-block-opens-one-entry.md).
_ENTRY_OPENING = re.compile(r"(?m)^(?=## \[|<!-- llm-wiki-operation:)")


def contained_block(text: str) -> str:
    """The block with every entry-opening line after its first escaped."""
    start = len(text) - len(text.lstrip("\n"))
    first_end = text.find("\n", start)
    if first_end == -1:
        return text
    return text[:first_end] + _ENTRY_OPENING.sub("\\\\", text[first_end:])


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
    text = contained_block(redact_secrets(text))
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
    text = contained_block(redact_secrets(text))
    block = f"\n{marker}\n{text}{'' if text.endswith(chr(10)) else chr(10)}".encode()
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
    day = local_now().strftime("%Y-%m-%d")
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


