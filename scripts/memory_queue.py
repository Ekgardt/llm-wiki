"""Operator commands for the memory queue and the processors its worker runs by hand."""

from __future__ import annotations

import json
import multiprocessing
import re
import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

import compile_progress
import queue_core
from llm_wiki import runtime_roots
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.queue_compile_recovery import operator_compile_recovery
from llm_wiki.queue_contracts import MAX_RUNTIME_ATTEMPTS as MAX_RUNTIME_ATTEMPTS
from llm_wiki.queue_contracts import STATES as STATES
from llm_wiki.queue_contracts import AttemptRecord as AttemptRecord
from llm_wiki.queue_contracts import CaptureTaskBinding as CaptureTaskBinding
from llm_wiki.queue_contracts import CompileCompletionError as CompileCompletionError
from llm_wiki.queue_contracts import CorruptExportProgress as CorruptExportProgress
from llm_wiki.queue_contracts import CorruptPurgeProgress as CorruptPurgeProgress
from llm_wiki.queue_contracts import DeadCaptureRedrive as DeadCaptureRedrive
from llm_wiki.queue_contracts import DeferredResult as DeferredResult
from llm_wiki.queue_contracts import LeaseFenceError as LeaseFenceError
from llm_wiki.queue_contracts import LeaseProcessor as LeaseProcessor
from llm_wiki.queue_contracts import MigrationBusy as MigrationBusy
from llm_wiki.queue_contracts import PurgeReceipt as PurgeReceipt
from llm_wiki.queue_contracts import QueueFailure as QueueFailure
from llm_wiki.queue_contracts import QueueLease as QueueLease
from llm_wiki.queue_contracts import QueueOperationError as QueueOperationError
from llm_wiki.queue_contracts import QueueTask as QueueTask
from llm_wiki.queue_contracts import RestoreReceipt as RestoreReceipt
from llm_wiki.queue_contracts import ResultConflictError as ResultConflictError
from llm_wiki.queue_contracts import SemanticDecision as SemanticDecision
from llm_wiki.queue_contracts import SourceFence as SourceFence
from llm_wiki.queue_contracts import WorkerSummary as WorkerSummary
from llm_wiki.queue_evidence_records import CorruptEvidence as CorruptEvidence
from llm_wiki.queue_task_lineage import require_dispatch_identity
from llm_wiki.queue_task_records import ExportedRows as ExportedRows
from llm_wiki.queue_validation import require_task_identifier, validate_worker_policy
from llm_wiki.queue_worker_state import ProcessorOutcome as ProcessorOutcome
from llm_wiki.queue_worker_state import QueueRetryPolicy as QueueRetryPolicy
from llm_wiki.queue_worker_state import WorkerProgress as WorkerProgress
from llm_wiki.queue_worker_state import processor_error_code as processor_error_code
from llm_wiki.reliability_defaults import DEFAULTS
from secret_redact import redact_secrets


def operator_status() -> dict[str, object]:
    tasks = queue_core.default_queue().list_tasks()
    states = {state: 0 for state in STATES}
    capabilities: set[str] = set()
    codes: set[str] = set()
    for task in tasks:
        states[task.state] += 1
        if task.blocked_capability:
            capabilities.add(task.blocked_capability)
        if task.error_code:
            codes.add(task.error_code)
    return {
        "counts": {"total": len(tasks)},
        "states": states,
        "capabilities": sorted(capabilities),
        "codes": sorted(codes),
    }


def _manual_query(payload: Mapping[str, Any]) -> bool | DeferredResult:
    """Answer one deferred query with the configured backend."""
    from llm_client import call_llm

    prompt = payload.get("prompt", "")
    if not prompt:
        return False
    result = call_llm(
        prompt,
        payload.get("system_prompt", ""),
        max_tokens=int(payload.get("max_tokens") or 4000),
    )
    if not result:
        return False
    return DeferredResult(result.encode("utf-8"))


def _round_trips_as_date(day: str) -> bool:
    """Whether the text is a calendar date that survives `%Y-%m-%d` both ways."""
    try:
        return datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") == day
    except ValueError:
        return False


def _valid_day(raw_day: object, now: datetime) -> str | None:
    """The day this flush belongs to, when it is a real calendar date."""
    day = now.strftime("%Y-%m-%d") if raw_day is None else raw_day
    if not isinstance(day, str):
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) is None:
        return None
    return day if _round_trips_as_date(day) else None


def daily_log_path(day: str) -> Path | None:
    """The daily log for this day, when it stays inside the daily directory."""
    root = runtime_roots.vault_root()
    daily_dir = (root / "knowledge" / "daily").resolve()
    daily_path = (daily_dir / f"{day}.md").resolve()
    try:
        daily_path.relative_to(daily_dir)
    except ValueError:
        return None
    return daily_path


def _flush_operation_id(payload: Mapping[str, Any]) -> str:
    """The same id for a flush and every redrive of it: the payload, not the task.

    See `docs/research/2026-09-14-a-redriven-flush-appends-nothing-new.md`.
    """
    return f"flush:{sha256_bytes(canonical_json_bytes(dict(payload)))}"


def _append_flush_block(
    daily_path: Path,
    payload: Mapping[str, Any],
    operation_id: str,
    result: str,
    now: datetime,
) -> None:
    """Append what the classifier judged worth keeping, once per flush."""
    from daily_log_append import locked_append_once
    from llm_wiki.classification_protocol import classify_response

    tier, body = classify_response(result)
    if tier == "ok" or not body:
        return
    session_id = payload.get("session_id", "deferred")
    event = payload.get("event", "session-end")
    block = (
        f"\n## [{now.strftime('%H:%M:%S')}] deferred-{event} | {session_id}\n"
        f"- Tier: `{tier}`\n\n{redact_secrets(body)}\n"
    )
    locked_append_once(daily_path, block, operation_id)


def _flush_target_path(payload: Mapping[str, Any], now: datetime) -> Path | None:
    """The daily log this flush may append to, when the day names one."""
    day = _valid_day(payload.get("day"), now)
    if day is None:
        return None
    return daily_log_path(day)


def _flush_inputs(payload: Mapping[str, Any], now: datetime):
    """The prompt and the daily log this flush writes, or None when either is absent."""
    prompt = payload.get("prompt", "")
    if not prompt:
        return None
    daily_path = _flush_target_path(payload, now)
    if daily_path is None:
        return None
    return prompt, daily_path


def _manual_flush(task: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    """Summarize one session into its daily log."""
    from iso_time import local_now
    from llm_client import call_llm

    now = local_now()
    inputs = _flush_inputs(payload, now)
    if inputs is None:
        return False
    prompt, daily_path = inputs
    result = call_llm(
        prompt,
        payload.get("system_prompt", ""),
        max_tokens=int(payload.get("max_tokens") or 1500),
    )
    if not result:
        return False
    _append_flush_block(daily_path, payload, _flush_operation_id(payload), result, now)
    return True


def manual_compile() -> bool:
    """Run one compile pass in a child process, for as long as one compile may run.

    The bound is the nightly's (`scheduled_nightly.compile_wait_seconds`, measured and
    operator-set); without one a hung compile held the queue worker for good (audit
    2026-09-27 C-9).
    """
    from nightly_budget import (
        compile_wait_seconds,
    )

    root = runtime_roots.vault_root()
    command = [sys.executable, str(root / "scripts" / "compile_memory.py"), "--trigger", "auto"]
    try:
        completed = subprocess.run(
            command, cwd=root, check=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=compile_wait_seconds(),
        )
    except subprocess.TimeoutExpired:
        return False
    return completed.returncode == 0


def _manual_query_task(
    _task: Mapping[str, Any], payload: Mapping[str, Any]
) -> bool | DeferredResult:
    return _manual_query(payload)


def _manual_compile_task(
    _task: Mapping[str, Any], _payload: Mapping[str, Any]
) -> bool | DeferredResult:
    return manual_compile()


_MANUAL_TASK_HANDLERS = {
    "query": _manual_query_task,
    "flush": _manual_flush,
    "compile": _manual_compile_task,
}


def manual_processor(task: dict[str, Any]) -> bool | DeferredResult:
    handler = _MANUAL_TASK_HANDLERS.get(task.get("type"))
    if handler is None:
        return False
    return handler(task, task.get("payload", {}))


def manual_lease_processor(queue, lease, deadline, task, *, lock_token=None, owner=None):
    if lease.kind != 'compile' or not queue_core.uses_compile_journal(queue):
        return manual_processor(task)
    from compile_leased_run import (
        leased_compile_processor,
    )

    return leased_compile_processor(queue, lease, deadline, task, lock_token=lock_token, owner=owner)


# Order matters: the first matching entry wins, so the narrower exception
# classes come before the broader ones they inherit from.
_CLI_ERROR_CODES: tuple[tuple[type[BaseException] | tuple[type[BaseException], ...], str], ...] = (
    (compile_progress.ProgressCorrupt, "compile_progress_corrupt"),
    (compile_progress.ProgressConflict, "compile_progress_conflict"),
    (KeyError, "task_not_found"),
    (PermissionError, "permission_denied"),
    (sqlite3.Error, "sqlite_error"),
    ((UnicodeError, json.JSONDecodeError), "decode_error"),
    (
        (
            subprocess.SubprocessError,
            multiprocessing.ProcessError,
            BrokenPipeError,
            EOFError,
            ChildProcessError,
        ),
        "process_error",
    ),
    (TimeoutError, "worker_timeout"),
    (OSError, "os_error"),
    ((TypeError, ValueError), "invalid_input"),
)


def _queue_operation_code(error: QueueOperationError) -> str:
    """The operation's own code, when it is one we are willing to print."""
    code = error.code
    if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
        return code
    return "internal_error"


def _cli_error_code(error: BaseException) -> str:
    if isinstance(error, QueueOperationError):
        return _queue_operation_code(error)
    for kinds, code in _CLI_ERROR_CODES:
        if isinstance(error, kinds):
            return code
    return "internal_error"


def _cli_error_detail(error: BaseException) -> str:
    """The boundary a queue failure names, redacted and bounded, or nothing."""
    if not isinstance(error, QueueOperationError):
        return ""
    return redact_secrets(error.detail)[:queue_core.MAX_CLI_DETAIL_CHARS]


def _cli_error_payload(error: BaseException) -> dict[str, object]:
    detail = _cli_error_detail(error)
    code = _cli_error_code(error)
    return {"codes": [code], "detail": detail} if detail else {"codes": [code]}


def emit_cli_error(error: BaseException) -> int:
    payload = canonical_json_bytes(_cli_error_payload(error))
    print(payload.decode("utf-8"))
    return 2


def _emit_invalid_arguments() -> int:
    payload = canonical_json_bytes({"ok": False, "code": "invalid_arguments"})
    print(payload.decode("utf-8"))
    return 2


def build_cli_parser() -> queue_core.RedactedArgumentParser:
    parser = queue_core.RedactedArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "list",
            "status",
            "compile-progress",
            "compile-recover",
            "work",
            "cancel",
            "redrive",
            "unblock",
            "purge",
            "redrive-dead-captures",
            "restore",
            "quarantine-corrupt",
            "purge-corrupt",
        ],
    )
    parser.add_argument("task_id", nargs="?")
    parser.add_argument("--max-tasks", type=int, default=DEFAULTS.worker_max_tasks)
    parser.add_argument("--max-seconds", type=int, default=DEFAULTS.worker_max_seconds)
    parser.add_argument("--min-claim-seconds", type=int, default=queue_core.WORKER_MIN_CLAIM_SECONDS)
    parser.add_argument("--idle-seconds", type=int, default=DEFAULTS.worker_idle_seconds)
    parser.add_argument("--lease-seconds", type=int, default=DEFAULTS.queue_lease_seconds)
    parser.add_argument(
        "--heartbeat-seconds", type=int, default=DEFAULTS.queue_heartbeat_seconds
    )
    parser.add_argument("--max-attempts", type=int, default=DEFAULTS.queue_max_attempts)
    parser.add_argument(
        "--retry-base-seconds", type=int, default=DEFAULTS.retry_base_seconds
    )
    parser.add_argument(
        "--retry-cap-seconds", type=int, default=DEFAULTS.retry_cap_seconds
    )
    parser.add_argument("--terminal-before")
    parser.add_argument("--changed-after")
    parser.add_argument("--export", type=Path)
    parser.add_argument(
        "--include-dead",
        action="store_true",
        help="Also purge attempts-exhausted tasks; they are retained by default.",
    )
    parser.add_argument("--reason")
    parser.add_argument("--work-id")
    parser.add_argument("--dispatch-digest")
    parser.add_argument("--accept-unknown-cost", action="store_true")
    return parser


def _cli_list(_args, _parser) -> int:
    records = [{"id": task.id, "state": task.state} for task in queue_core.default_queue().list_tasks()]
    print(json.dumps(records, sort_keys=True))
    return 0


def _cli_status(_args, _parser) -> int:
    print(json.dumps(operator_status(), sort_keys=True))
    return 0


def _cli_compile_progress(args, _parser) -> int:
    task_id = _require_cli_task_id(args)
    queue = _compile_queue_for_cli()
    print(json.dumps(queue.inspect_compile_progress(task_id), sort_keys=True))
    return 0


def _compile_queue_for_cli():
    queue = queue_core.active_memory_queue(runtime_roots.vault_root(), runtime_roots.state_root())
    if not queue_core.uses_compile_journal(queue):
        raise QueueOperationError('compile_progress_upgrade_required')
    return queue


def _cli_compile_recover(args, _parser) -> int:
    from llm_wiki.compile_call_runtime import resolve_model_call
    from operational_ownership import OperationalOwnershipError, heartbeat_owner

    task_id = _require_cli_task_id(args)
    require_task_identifier(task_id)
    compile_progress.require_digest(args.work_id)
    compile_progress.require_digest(args.dispatch_digest)
    authorization = _cli_compile_consent(args)
    validate_worker_policy(args.lease_seconds, args.heartbeat_seconds, args.max_attempts,
                            args.retry_base_seconds, args.retry_cap_seconds)
    queue = _compile_queue_for_cli()
    dispatch = queue.get_compile_dispatch(args.work_id)
    require_dispatch_identity(dispatch, args.dispatch_digest)
    resolve_call = partial(resolve_model_call, authorization=authorization)
    try:
        result = operator_compile_recovery(queue, task_id, dispatch, resolve_call, args, heartbeat_owner)
    except OperationalOwnershipError as error:
        reason = _queue_operation_code(QueueOperationError(error.code))
        raise QueueOperationError('compile_recovery_owner_unavailable', reason) from error
    print(json.dumps(result, sort_keys=True))
    return 0


def _cli_compile_consent(args):
    import getpass

    if not args.accept_unknown_cost:
        return None
    if not isinstance(args.reason, str) or not args.reason.strip():
        raise QueueOperationError('compile_repeat_reason_required')
    return {'dispatch_digest': args.dispatch_digest, 'actor': getpass.getuser(), 'reason': args.reason}


def cli_work(args, parser) -> int:
    try:
        validate_worker_policy(
            args.lease_seconds,
            args.heartbeat_seconds,
            args.max_attempts,
            args.retry_base_seconds,
            args.retry_cap_seconds,
        )
    except ValueError as exc:
        parser.error(str(exc))
    summary = queue_core.run_worker(
        LeaseProcessor(manual_lease_processor),
        max_tasks=args.max_tasks,
        max_seconds=args.max_seconds,
        idle_seconds=args.idle_seconds,
        lease_seconds=args.lease_seconds,
        heartbeat_seconds=args.heartbeat_seconds,
        max_attempts=args.max_attempts,
        retry_base_seconds=args.retry_base_seconds,
        retry_cap_seconds=args.retry_cap_seconds,
        min_claim_seconds=args.min_claim_seconds,
    )
    counts = {
        "dead": summary.dead,
        "failed": summary.failed,
        "processed": summary.processed,
        "remaining_eligible": summary.remaining_eligible,
        "skipped": summary.skipped,
        "succeeded": summary.succeeded,
    }
    print(json.dumps({"counts": counts}, sort_keys=True))
    if summary.failed or summary.dead or summary.remaining_eligible:
        return 1
    return 0


def _require_cli_task_id(args) -> str:
    if not args.task_id:
        raise QueueOperationError("task_id_required")
    return str(args.task_id)


def _cli_quarantine_corrupt(args, _parser) -> int:
    task_id = _require_cli_task_id(args)
    if not isinstance(args.reason, str):
        raise ValueError("quarantine reason is invalid")
    if not 1 <= len(args.reason.encode("utf-8")) <= 4096:
        raise ValueError("quarantine reason is invalid")
    queue = queue_core.v3_queue_for_cli()
    with queue_core.repair_owner_for_cli(queue) as owner:
        progress = queue.quarantine_corrupt(task_id, reason=args.reason, owner=owner)
    print(
        json.dumps(
            {
                "code": progress.code,
                "complete": progress.complete,
                "operation_id": progress.operation_id,
                "page_count": progress.pages_written,
                "state": progress.state,
                "task_id": progress.task_id,
            },
            sort_keys=True,
        )
    )
    return 0


def _cli_purge_corrupt(args, _parser) -> int:
    task_id = _require_cli_task_id(args)
    queue = queue_core.v3_queue_for_cli()
    with queue_core.repair_owner_for_cli(queue) as owner:
        progress = queue.purge_quarantined(task_id, owner=owner)
    print(
        json.dumps(
            {
                "code": progress.code,
                "complete": progress.complete,
                "links_deleted": progress.links_deleted,
                "operation_id": progress.operation_id,
                "page_count": progress.pages_written,
                "state": progress.state,
                "task_id": progress.task_id,
            },
            sort_keys=True,
        )
    )
    return 0


def _cli_cancel(args, _parser) -> int:
    task_id = _require_cli_task_id(args)
    changed = queue_core.cancel(task_id)
    state = "unchanged"
    if changed:
        state = queue_core.default_queue().get(task_id).state
    print(json.dumps({"id": task_id, "state": state}, sort_keys=True))
    if changed:
        return 0
    return 1


def _cli_unblock(args, _parser) -> int:
    task_id = _require_cli_task_id(args)
    queue_core.default_queue().unblock(task_id)
    print(json.dumps({"id": task_id, "state": "ready"}, sort_keys=True))
    return 0


def _cli_redrive(args, _parser) -> int:
    task_id = queue_core.redrive(_require_cli_task_id(args))
    print(json.dumps({"id": task_id, "state": "ready"}, sort_keys=True))
    return 0


def _cli_terminal_before(args) -> datetime:
    if args.terminal_before is None:
        raise QueueOperationError("terminal_before_required")
    try:
        return datetime.fromisoformat(args.terminal_before)
    except ValueError:
        raise QueueOperationError("terminal_before_invalid") from None


def _cli_purge(args, _parser) -> int:
    terminal_before = _cli_terminal_before(args)
    if args.export is None:
        raise QueueOperationError("export_required")
    receipt = queue_core.purge(
        terminal_before=terminal_before,
        export_path=args.export,
        include_dead=args.include_dead,
    )
    print(
        json.dumps(
            {
                "counts": {"purged": receipt.purged, "retained": len(receipt.retained)},
                "ids": list(receipt.task_ids),
                "retained": list(receipt.retained),
            },
            sort_keys=True,
        )
    )
    return 0


def _cli_redrive_dead_captures(args, _parser) -> int:
    if args.changed_after is None:
        raise QueueOperationError("changed_after_required")
    try:
        changed_after = datetime.fromisoformat(args.changed_after)
    except ValueError:
        raise QueueOperationError("changed_after_invalid") from None
    outcome = queue_core.v3_queue_for_cli().redrive_dead_captures(changed_after=changed_after)
    print(
        json.dumps(
            {
                "redriven": [{"dead": dead, "ready": ready} for dead, ready in outcome.redriven],
                "refused": [{"dead": dead, "code": code} for dead, code in outcome.refused],
            },
            sort_keys=True,
        )
    )
    return 0


def _cli_restore(args, _parser) -> int:
    if args.export is None:
        raise QueueOperationError("export_required")
    restored = queue_core.restore(export_path=args.export)
    print(
        json.dumps(
            {
                "counts": {"restored": restored.restored},
                "ids": [
                    {"exported": exported, "restored": current}
                    for exported, current in restored.task_ids
                ],
            },
            sort_keys=True,
        )
    )
    return 0


CLI_COMMANDS = {
    "list": _cli_list,
    "status": _cli_status,
    "compile-progress": _cli_compile_progress,
    "compile-recover": _cli_compile_recover,
    "work": cli_work,
    "quarantine-corrupt": _cli_quarantine_corrupt,
    "purge-corrupt": _cli_purge_corrupt,
    "cancel": _cli_cancel,
    "redrive": _cli_redrive,
    "unblock": _cli_unblock,
    "purge": _cli_purge,
    "redrive-dead-captures": _cli_redrive_dead_captures,
    "restore": _cli_restore,
}


def cli() -> int:
    parser = build_cli_parser()
    try:
        args = parser.parse_args()
        handler = CLI_COMMANDS.get(args.command)
        if handler is None:
            return 2
        return handler(args, parser)
    except queue_core.InvalidArguments:
        return _emit_invalid_arguments()
    except Exception as exc:  # noqa: BLE001 - CLI is a redacted trust boundary
        return emit_cli_error(exc)


if __name__ == "__main__":
    raise SystemExit(cli())
