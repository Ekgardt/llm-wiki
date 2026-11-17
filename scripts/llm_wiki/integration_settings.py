"""Delegates, timeouts and limits shared by the agent integration adapter."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from daily_log_append import BACKGROUND_APPEND_BUDGET_SECONDS, BREADCRUMB_APPEND_BUDGET_SECONDS

SCRIPTS_DIR = Path(__file__).resolve().parents[1]


# A delegate without its own budget stops here, inside the host hook's budget (see the note below).
DELEGATE_TIMEOUT_SECONDS = 10


# Delegates on the host's 5-second hooks stop before the host stops the hook, so
# a hang is recorded here instead of vanishing with the process (audit B-11,
# docs/research/2026-09-25-a-hook-stops-its-delegate-before-the-host-stops-it.md).
# One rule for both: the append's own budget plus time to start the interpreter.
# The prompt delegate was stopped at 2.5 s while its append could try for 3.0 s
# (audit 2026-09-26 C-1, docs/research/2026-09-26-a-hook-budget-is-one-sum.md).
HOST_HOOK_TIMEOUT_SECONDS = 5.0

# The interpreter start-up half of that sum, shared by the foreground and background delegates.
DELEGATE_STARTUP_SECONDS = 1.0


_BREADCRUMB_DELEGATE_TIMEOUT = BREADCRUMB_APPEND_BUDGET_SECONDS + DELEGATE_STARTUP_SECONDS


DELEGATE_TIMEOUTS = {
    "user_prompt_capture.py": _BREADCRUMB_DELEGATE_TIMEOUT,
    "post_tool_capture.py": _BREADCRUMB_DELEGATE_TIMEOUT,
}


# A hook the host runs in the background (`async: true`) has no host limit to stay
# under; its delegate gets the background append budget and the same start-up time
# (docs/research/2026-09-29-a-tool-breadcrumb-waits-in-the-background.md).
BACKGROUND_DELEGATE_TIMEOUTS = {
    "post_tool_capture.py": BACKGROUND_APPEND_BUDGET_SECONDS + DELEGATE_STARTUP_SECONDS,
}


# The unattended queue drain's ceiling; the nightly step it runs in is bounded separately. basis unknown — value predates measurement; review when the nightly queue step reports this drain timing out.
MAINTENANCE_DRAIN_TIMEOUT_SECONDS = 600


# Settled captures one unattended drain works through before it reports. basis unknown — value predates measurement; review when capture drains end with work still queued.
CAPTURE_DRAIN_MAX_TASKS = 20


# The capture drain stops before MAINTENANCE_DRAIN_TIMEOUT_SECONDS (600) kills it, so it can report. basis unknown — value predates measurement; review when capture drains end unfinished.
CAPTURE_DRAIN_SECONDS = 450


# Inline transcript text an integration event may carry instead of a transcript path. basis unknown — value predates measurement; review when hosts send longer inline transcripts.
MAX_TRANSCRIPT_TEXT_CHARS = 8000


# One host event, not one tool result. Claude Code sends `tool_response` in the
# PostToolUse payload -- the tool's own output -- and this adapter reads none of
# it: `tool_payload` takes `tool_name` and one path or command out of
# `tool_input`. At 64 KiB the adapter refused any edit or Bash call whose output
# was larger, which on 2026-08-26 lost eight `post_tool_use` captures on this
# machine. The bound stays because a hook must not read without one; it is now
# the size of one bounded record this runtime already stores.
MAX_STDIN_BYTES = 1024 * 1024


# Exclusive creates of a transient transcript under fresh 128-bit random names; a collision is
# practically impossible, so repeated failure means the directory is hostile and the capture stops.
TRANSIENT_CREATE_ATTEMPTS = 10


# The transcript evidence one capture carries. It must fit inside the capture intent
# (memory_state.MAX_CAPTURE_INTENT_BYTES, 1 MiB) with room for the intent's own
# fields; 900 KiB leaves 124 KiB for them. The split is inferred from the two values,
# not measured; review if an intent near the cap is refused.
MAX_CAPTURE_EVIDENCE_BYTES = 900 * 1024


SOURCES = frozenset({"claude", "opencode", "codex"})


EVENTS = frozenset(
    {"session_start", "session_end", "pre_compact", "stop", "user_prompt", "post_tool_use"}
)


def build_session_start_context(slug: str | None = None) -> Sequence[Any]:
    """Build structured context without loading SessionStart on unrelated commands.

    The slug is this session's own project, so its advisory and guard rails are not
    the ones of whichever session wrote the last heartbeat.
    """
    from session_start_context import build_context_items

    return build_context_items(slug)


CHECKPOINT_SIGNAL_FIELDS = frozenset(
    {
        "checkpoint_type",
        "dirty",
        "changed",
        "significant",
        "decision",
        "correction",
        "blocker_opened",
        "blocker_closed",
        "task_completed",
        "task_cancelled",
        "ownership_transferred",
        "significant_failure",
        "public_contract_changed",
        "test_result_changed",
        "token_percent",
        "compaction_confirmed",
        "project_delta",
        "branch",
    }
)


DELEGATES = frozenset(
    {
        "session_start_context.py",
        "session_start_project_state.py",
        "session_end_project_tag.py",
        "user_prompt_capture.py",
        "post_tool_capture.py",
        "heartbeat_record.py",
    }
)


# The two thin wrappers that used to spawn the detached flush. They were deleted on
# 2026-09-17 — the adapter captures these events itself — and this map outlives them
# as a compatibility guard: an older `settings.json` that still passes one of these
# flags is ignored here instead of failing on a delegate that no longer exists. See
# `docs/research/2026-09-17-the-two-hook-wrappers-nothing-calls-are-retired.md`.
CAPTURE_DELEGATES = {
    "pre_compact": "precompact_capture.py",
    "session_end": "session_end_capture.py",
}


# The delegate `ingest_event` itself runs for an event. Claude's hooks name it with
# `--delegate`, and the event takes the same ingest path as on every other host
# (audit B-7, docs/research/2026-09-25-a-claude-prompt-reaches-feedback-capture.md).
# Feedback candidates were retired on 2026-09-25: corrections are learned by
# compile from the daily log
# (docs/research/2026-09-25-corrections-are-learned-by-compile-not-by-candidates.md).
INGESTED_DELEGATES = {
    **CAPTURE_DELEGATES,
    "user_prompt": "user_prompt_capture.py",
    "post_tool_use": "post_tool_capture.py",
}

