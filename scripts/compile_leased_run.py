"""One compile task leased from the queue, from capture to execution."""
from __future__ import annotations

from dataclasses import replace
from functools import partial

from compile_pipeline import (
    prepare_compile_planning,
    restore_compile_planning,
    retire_stale_source_failures,
)
from compile_receipts import receipt_predicate, require_compile_active
from llm_wiki import runtime_roots
from llm_wiki.compile_pipeline_claims import (
    DROPPED_CLAIMS,
)
from llm_wiki.compile_pipeline_selection import (
    compile_under_lock,
    finish_leased_compile,
    leased_compile_args,
    leased_compile_request,
    repair_compile_mirror,
    require_leased_compile_queue,
    require_resolved_run_planning,
    select_dailies,
)
from markdown_transaction import active_or_legacy_coordinator
from rebuild_memory_index import build_index_bytes


def leased_compile_processor(queue, lease, deadline, task, *, lock_token=None, owner=None):
    from llm_wiki.queue_contracts import LeaseFenceError
    from queue_core import compat_task

    if task != compat_task(lease):
        raise LeaseFenceError('compatibility payload differs from compile lease')
    return run_leased_compile(queue, lease, deadline=deadline, lock_token=lock_token, owner=owner) == 0


def run_leased_compile(
    queue, lease, *, deadline=float('inf'), cancelled=None, owner=None, lock_token=None
):
    """Execute an adopted compile task; the worker parent still owns settlement."""
    from llm_wiki.compile_controller_capture import require_capture_vault

    require_compile_active(deadline, cancelled)
    require_leased_compile_queue(queue, lease)
    require_capture_vault(queue)
    args = leased_compile_args(lease)
    args.lock_token = lock_token  # Ephemeral parent authority, never retained user intent.
    return compile_under_lock(args, deadline=deadline, cancelled=cancelled, owner=owner,
                               execute=partial(_run_leased_compile, queue, lease))


def _run_leased_compile(queue, lease, args, *, deadline, cancelled, owner):
    from llm_wiki.compile_controller_capture import admit_compile_capture, read_compile_execution
    from llm_wiki.compile_controller_dependencies import capture_compile_dependencies
    from llm_wiki.compile_controller_work import execute_compile_work

    require_compile_active(deadline, cancelled)
    DROPPED_CLAIMS.clear()
    coordinator = active_or_legacy_coordinator(runtime_roots.vault_root(), runtime_roots.state_root(), deadline=deadline)
    prepare = partial(_prepare_leased_capture, args, coordinator, deadline)
    snapshot = admit_compile_capture(queue, lease, prepare, deadline=deadline)
    request, retained = read_compile_execution(queue, lease, snapshot, deadline=deadline)
    planning = restore_compile_planning(retained, deadline=deadline)
    if request != leased_compile_request(args, planning):
        raise ValueError('retained compile planning disagrees with request')
    dependencies = capture_compile_dependencies(runtime_roots.vault_root(), request, deadline=deadline)
    completed = execute_compile_work(queue, lease, snapshot, coordinator=coordinator,
        dependencies=dependencies, request=request, plan=planning.batches, trigger=args.trigger,
        owner=owner, deadline=deadline, cancelled=cancelled)
    return finish_leased_compile(args, completed)


def _prepare_leased_capture(args, coordinator, deadline):
    from llm_wiki.compile_controller_dependencies import capture_compile_dependencies

    planning = prepare_compile_planning(deadline=deadline)
    require_resolved_run_planning(planning)
    request = leased_compile_request(args, planning)
    dependencies = capture_compile_dependencies(runtime_roots.vault_root(), request, deadline=deadline)
    prepared = _capture_for_leased_run(args, coordinator, dependencies, request, deadline)
    return replace(prepared, execution=planning.retained())


def _capture_for_leased_run(args, coordinator, dependencies, request, deadline):
    from llm_wiki.compile_controller_capture import CompileCapturePreparation

    selection = receipt_predicate(coordinator, deadline=deadline)
    paths = select_dailies(args, {}, coordinator=coordinator, deadline=deadline, selection=selection)
    repair_compile_mirror(coordinator, deadline=deadline, selection=selection)
    retire_stale_source_failures(coordinator.state_root, coordinator=coordinator,
                                 deadline=deadline, selection=selection)
    if paths:
        build_index_bytes(runtime_roots.vault_root())
    return CompileCapturePreparation(paths, dependencies, request, compiled=selection)
