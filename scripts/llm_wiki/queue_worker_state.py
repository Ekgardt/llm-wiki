"""Worker values and progress; execution, SQL and clocks belong to callers."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import NamedTuple

from llm_wiki.queue_contracts import DeferredResult, QueueOperationError
from llm_wiki.reliability_defaults import DEFAULTS
from llm_wiki.utf8_text import _utf8_prefix


class QueueRetryPolicy(NamedTuple):
    """Retry bounds only the legacy queue takes; the adopted one settles its own."""

    max_attempts: int = DEFAULTS.queue_max_attempts
    retry_base_seconds: int = DEFAULTS.retry_base_seconds
    retry_cap_seconds: int = DEFAULTS.retry_cap_seconds


PROCESSOR_ERROR_CODE_BYTES = 64  # `attempt_history.error_code` CHECK in the v3 schema


_CODE_SHAPED_MESSAGE = re.compile(r"[a-z][a-z0-9_]{0,62}")


def processor_error_code(error: BaseException) -> str:
    """The stable code a failed processor leaves in the attempt history.

    A queue error carries its own code. Any other exception names its type, or
    its message when the message is itself a code (`RuntimeError("intent_fence_lost")`
    from the transaction layer). Never the message text: it may carry paths or
    content. On the live vault every one of 225 failed attempts said only
    `processor_failed` (2026-09-23, audit C3).
    """
    if isinstance(error, QueueOperationError):
        return error.code
    message = str(error)
    reason = message if _CODE_SHAPED_MESSAGE.fullmatch(message) else type(error).__name__
    code = f"processor_failed:{reason}"[:PROCESSOR_ERROR_CODE_BYTES]
    return _utf8_prefix(code, PROCESSOR_ERROR_CODE_BYTES)


class _ProcessorOutcome(NamedTuple):
    outcome: bool | DeferredResult
    timed_out: bool
    cleanup_failed: bool
    error_code: str | None = None


@dataclass
class _WorkerProgress:
    """What a worker has done, and when it last had something to do."""

    totals: dict[str, int]
    processed: int = 0
    idle_started: float | None = None

    def advance(self, counts: Mapping[str, int]) -> str:
        """`worked`, `halted` or `idle` after recording one turn."""
        handled = counts["ok"] + counts["failed"]
        for key in self.totals:
            self.totals[key] += counts[key]
        if not handled:
            return "idle"
        self.processed += handled
        self.idle_started = None
        return "halted" if counts.get("halted") else "worked"


# Preserve existing import and serialization identities, including private records.
for _value in (QueueRetryPolicy, _ProcessorOutcome, _WorkerProgress, processor_error_code):
    _value.__module__ = "memory_queue"
_WorkerProgress.advance.__module__ = "memory_queue"
del _value
