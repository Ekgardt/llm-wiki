"""Compile knowledge/daily/*.md into knowledge/notes/* durable pages.

CLI:
    uv run python scripts/compile_memory.py              # compile changed daily logs
    uv run python scripts/compile_memory.py --all        # deprecated, does nothing and
                                                         # says so: a day with a
                                                         # committed receipt is never
                                                         # compiled again
    uv run python scripts/compile_memory.py --file PATH  # compile one daily log
    uv run python scripts/compile_memory.py --dry-run    # plan only, no writes
    uv run python scripts/compile_memory.py --trigger auto|manual
                                                         # records invocation source in
                                                         # state.json; `auto` is set by
                                                         # flush_memory.py when the 18:00
                                                         # hook spawns this compile, any
                                                         # direct CLI run defaults to
                                                         # `manual`. Surfaces as
                                                         # "Automated compile pass" vs
                                                         # "Manual compile pass" in
                                                         # knowledge/log.md.

Incrementality:
    Durable v2 receipts under knowledge/daily/receipts are authoritative. The
    `compiled_daily_hashes` state field is only a post-commit diagnostic mirror.

Pages, the in-process index, log entry, and receipts commit in one recoverable transaction.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING

import compile_lock
import compile_pipeline
import settings
from compile_leased_run import leased_compile_processor
from llm_wiki import compile_pipeline_selection, runtime_roots

if TYPE_CHECKING:
    from operational_ownership import OwnerLease


def main() -> int:
    args = compile_pipeline_selection.parse_args()
    compile_pipeline_selection.report_deprecated_flags(args)
    if args.discard_unusable_receipts:
        discarded = compile_pipeline_selection.discard_unusable_receipts()
        print(f"discarded {len(discarded)} unusable receipt(s)")
        return 0
    code = compile_pipeline_selection.compile_under_lock(args, execute=_default_compile_executor(args))
    if not args.dry_run:
        refresh_generation_after_compile()
    return code


# A compiled page is searchable once the memory generation holds it. Refreshing
# after a completed attempt includes committed pages even when another batch
# failed. It is a no-op (`status: current`) when nothing changed and
# defers when another generation refresh holds the maintenance fence. See
# `docs/research/2026-09-24-an-answer-says-how-old-its-index-is.md`.
def refresh_generation_after_compile() -> None:
    """Make what the compile wrote searchable now, not after the next nightly."""
    from generation_maintenance import (
        run_generation_maintenance,
    )
    from secret_redact import describe_error

    seconds = settings.setting_value("generation.post_compile_seconds", runtime_roots.vault_root())
    try:
        outcome = run_generation_maintenance(
            runtime_roots.vault_root(), runtime_roots.state_root(), time_budget_seconds=seconds
        )
    except Exception as error:  # noqa: BLE001 - retain the separate compile outcome
        print(f"compile_memory: generation refresh failed: {describe_error(error)}", file=sys.stderr)
        return
    print(f"compile_memory: generation {outcome.get('status')} ({outcome.get('reason') or 'none'})")


def _default_compile_executor(args):
    return compile_pipeline.run if getattr(args, 'dry_run', False) else run_queued_compile


def run_queued_compile(args, *, deadline, cancelled, owner):
    from queue_core import (
        active_memory_queue,
        uses_compile_journal,
    )

    compile_pipeline.require_compile_active(deadline, cancelled)
    queue = active_memory_queue(runtime_roots.vault_root(), runtime_roots.state_root(), deadline=deadline)
    if not uses_compile_journal(queue):
        raise ValueError('foreground compile requires adopted compile journal')
    payload = compile_pipeline_selection.compile_task_payload(args)
    with queue.queue_owner(role='queue-worker', scope='compile', parent=owner) as execution_owner:
        return _owned_foreground_compile(queue, args, payload, execution_owner,
                                         deadline=deadline, cancelled=cancelled)


def _foreground_stopped(cancelled, lost):
    return lost.is_set() or (cancelled is not None and cancelled())


def foreground_processor(lock_token, owner):
    from llm_wiki.queue_contracts import LeaseProcessor

    return LeaseProcessor(partial(leased_compile_processor, lock_token=lock_token, owner=owner))


def _owned_foreground_compile(queue, args, payload, owner, *, deadline, cancelled):
    import threading
    from types import SimpleNamespace

    from operational_ownership import heartbeat_owner

    lost = threading.Event()
    stopped = partial(_foreground_stopped, cancelled, lost)
    renewer = SimpleNamespace(heartbeat=queue.heartbeat_queue_owner)
    with heartbeat_owner(owner, registry=renewer, lost=lost):
        task_id, lease = queue.claim_compile_request(payload, owner=f'compile-{os.getpid()}',
                                                     deadline=deadline, cancelled=stopped)
        processor = foreground_processor(compile_lock.lock_owner_token(), owner)
        task = _execute_foreground_task(queue, task_id, lease, processor, deadline, stopped)
        return _foreground_compile_result(args, task)


def _execute_foreground_task(queue, task_id, lease, processor, deadline, cancelled):
    from llm_wiki.reliability_defaults import DEFAULTS
    from queue_core import (
        run_processor_child,
        work_claimed,
    )

    work_claimed(queue, processor, run_processor_child, lease,
        deadline=deadline, monotonic=time.monotonic, cancelled=cancelled,
        lease_seconds=DEFAULTS.queue_lease_seconds, heartbeat_seconds=DEFAULTS.queue_heartbeat_seconds,
        max_attempts=DEFAULTS.queue_max_attempts, retry_base_seconds=DEFAULTS.retry_base_seconds,
        retry_cap_seconds=DEFAULTS.retry_cap_seconds)
    return queue.get(task_id)


def _foreground_compile_result(args, task):
    if task.state == 'succeeded':
        print(f'compile_memory: queue task {task.id} completed.', file=sys.stderr)
        return 0
    reason = f'queue task {task.id}: {task.state} ({task.blocked_capability or task.error_code or "not_claimable"})'
    compile_pipeline_selection.mark_finished(args.trigger, 'error', reason)
    print(f'compile_memory: {reason}', file=sys.stderr)
    return 1


def run_pending_compile(
    *,
    trigger: str = "manual",
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
    owner: OwnerLease | None = None,
) -> int:
    """Compile pending daily logs in-process under caller-owned bounds."""
    if trigger not in {"auto", "manual"}:
        raise ValueError("compile trigger must be auto or manual")
    args = argparse.Namespace(file=None, all=False, dry_run=False, trigger=trigger)
    return compile_pipeline_selection.compile_under_lock(
        args,
        deadline=deadline,
        cancelled=cancelled,
        owner=owner,
        execute=_default_compile_executor(args),
    )


if __name__ == "__main__":
    raise SystemExit(main())
