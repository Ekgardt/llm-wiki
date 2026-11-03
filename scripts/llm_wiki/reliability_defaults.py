"""Existing immutable reliability policy, without runtime or IO dependencies."""
from __future__ import annotations

from dataclasses import dataclass


# The Reliability v3 contract's coordination values, fixed in its plan
# (docs/superpowers/plans/2026-07-13-reliable-memory.md) and pinned by
# tests/test_reliable_memory.py. They are not operator settings (law 9 class c, kept):
# leases and heartbeats are pairs whose ratio every live owner relies on (a heartbeat
# at a third of its lease), attempt counts, busy waits and worker bounds are the
# plan's, and retention days are the contract's undo/archive windows (CLAUDE.md,
# Stage 2: "Archives keep 90 hot days"). Changing one means changing the contract.
@dataclass(frozen=True)
class ReliableMemoryDefaults:
    markdown_busy_ms: int = 10_000
    queue_busy_ms: int = 5_000
    transaction_retention_days: int = 30
    artifact_retention_days: int = 30
    archive_hot_days: int = 90
    project_lease_seconds: int = 30
    project_heartbeat_seconds: int = 10
    checkpoint_debounce_seconds: int = 30
    checkpoint_fallback_events: int = 20
    queue_lease_seconds: int = 120
    queue_heartbeat_seconds: int = 40
    queue_max_attempts: int = 8
    retry_base_seconds: int = 30
    retry_cap_seconds: int = 3_600
    worker_max_tasks: int = 20
    worker_max_seconds: int = 600
    worker_idle_seconds: int = 2
    priority_min: int = -100
    priority_max: int = 100
    queue_result_retention_days: int = 30
    dead_task_retention_days: int | None = None


ReliableMemoryDefaults.__module__ = "reliable_memory"
DEFAULTS = ReliableMemoryDefaults()


# One capture intent on disk; the hook that writes it and the worker that
# reads it bound the same file (`docs/research/2026-09-23-one-limit-one-place.md`).
# The largest installed intent measured 2026-09-27 was 977 392 bytes (93%);
# revisit this transport policy if intents approach it. Repair uses this same bound.
MAX_CAPTURE_INTENT_BYTES = 1024 * 1024


# The one bound on a capture decision file, for the writer and every reader. The
# indexer read it with 64 KiB while the writer allowed 1 MiB, so a verbose classifier
# answer (43 KB, stored twice: the wire answer and the plan) failed every retry and
# the day's summary was lost (audit 2026-09-27 B-3,
# docs/research/2026-09-27-one-file-one-bound.md). 1 MiB is the writer's bound, kept:
# about 170 times the 1 500-token answer the classifier asks for, so it refuses only an
# answer no summary needs, and CLI providers do not enforce the request's token cap.
MAX_CAPTURE_DECISION_BYTES = 1024 * 1024
