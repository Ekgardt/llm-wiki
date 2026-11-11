"""Worst-case seconds of the weekly pass, without the pass itself."""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import nightly_budget
from llm_wiki import runtime_roots

# Reflection calls the model once per page; the pass stops starting pages after this.
# See `docs/research/2026-09-14-the-weekly-task-outlasts-its-pass.md`.
REFLECTION_BUDGET_SECONDS = 1800
CONTRADICTIONS_STEP_SECONDS = 1800
# A weekly that met the nightly holding the shared fence skipped the whole week
# (audit 2026-09-26 B-20). It now asks again every minute, for as long as the
# nightly can run by its own bounds; the scheduler's limit counts that wait.
# Research: docs/research/2026-09-26-a-weekly-waits-for-the-nightly.md
FENCE_POLL_SECONDS = 60.0


# The queue keeps finished work for `queue_result_retention_days`; the designed
# purge exports it (intents and decisions included) to the private raw archive and
# only then deletes it from `run/`. Nothing ran it until 2026-09-24. See
# `docs/research/2026-09-24-every-store-has-a-bound.md`.


def queue_archive_dir() -> Path:
    return runtime_roots.vault_root() / "knowledge" / "raw" / "queue-archive"


def _queue_purge_command(script: Path) -> list[str]:
    from llm_wiki.reliability_defaults import DEFAULTS

    now = datetime.now(UTC)
    before = now - timedelta(days=DEFAULTS.queue_result_retention_days)
    return [
        sys.executable,
        str(script / "memory_queue.py"),
        "purge",
        "--terminal-before",
        before.isoformat(timespec="seconds"),
        "--export",
        str(queue_archive_dir() / now.strftime("%Y-%m-%d")),
        "--include-dead",
    ]


def script_steps() -> list[tuple[str, str, list[str], int]]:
    """(message, label, command, timeout) for every subprocess step, in order."""
    script = runtime_roots.vault_root() / "scripts"
    return [
        (
            "OKF conformance sweep (migrate_to_okf --apply)...",
            "okf",
            [sys.executable, str(script / "migrate_to_okf.py"), "--apply"],
            120,
        ),
        (
            "reporting memory queue status...",
            "status",
            [sys.executable, str(script / "memory_queue.py"), "status"],
            60,
        ),
        (
            "archiving and purging queue work finished past its retention...",
            "queue_purge",
            _queue_purge_command(script),
            600,
        ),
        (
            "auto-archiving stale pages (>180 days)...",
            "archive",
            [sys.executable, str(script / "archive_stale.py"), "--days", "180", "--apply"],
            120,
        ),
        (
            "archiving session records (>90 days)...",
            "sessions",
            [sys.executable, str(script / "archive_sessions.py"), "--apply"],
            300,
        ),
        (
            # "Archives keep 90 hot days" is a contract, and nothing ran the
            # archiver, so `knowledge/daily/` grew without bound and every
            # compile trigger hashed all of it. Research:
            # docs/research/2026-09-18-a-pass-that-knows-how-long-it-can-be.md
            "archiving daily logs past the hot window...",
            "daily_archive",
            [sys.executable, str(script / "archive_daily.py"), "--commit"],
            600,
        ),
        (
            "pruning superseded evidence-graph generations...",
            "generations",
            # Its own budget ends two minutes before this step is killed. See
            # `docs/research/2026-09-14-a-prune-inside-its-step.md`.
            [sys.executable, str(script / "prune_generations.py"), "--apply", "--budget-seconds", "1080"],
            1200,
        ),
    ]


def worst_case_seconds() -> float:
    """The longest the weekly pass can run by its own bounds; the scheduler's limit sits above it."""
    from llm_client import DEFAULT_TIMEOUT_S

    steps = sum(timeout for _message, _label, _command, timeout in script_steps())
    reflection = REFLECTION_BUDGET_SECONDS + DEFAULT_TIMEOUT_S
    waits = nightly_budget.COMPILE_IDLE_WAIT_SECONDS + fence_wait_seconds() + FENCE_POLL_SECONDS
    return float(waits + steps + CONTRADICTIONS_STEP_SECONDS + reflection)


def fence_wait_seconds() -> float:
    return nightly_budget.worst_case_seconds()
