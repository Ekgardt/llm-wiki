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
