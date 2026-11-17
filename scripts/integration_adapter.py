"""Normalize native host lifecycle events before existing capture pipelines."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from functools import partial
from pathlib import Path
from typing import Any

import memory_state
import project_checkpoint
from event_envelope import EventEnvelope
from llm_wiki import integration_delegates, runtime_roots
from llm_wiki.integration_capture_payload import (
    canonical_capture_payload as canonical_capture_payload,
)
from llm_wiki.integration_capture_records import capture_running_session as capture_running_session
from llm_wiki.integration_capture_records import capture_source_record as capture_source_record
from llm_wiki.integration_capture_records import encoded_capture_record as encoded_capture_record
from llm_wiki.integration_capture_records import (
    publish_capture_intent_from_payload as publish_capture_intent_from_payload,
)
from llm_wiki.integration_capture_records import wake_capture_worker as wake_capture_worker
from llm_wiki.integration_delegates import delegate_timeout as delegate_timeout
from llm_wiki.integration_delegates import delivery_arguments as delivery_arguments
from llm_wiki.integration_delegates import record_failed_delegate as record_failed_delegate
from llm_wiki.integration_events import normalize_event as normalize_event
from llm_wiki.integration_events import normalize_occurrence_event as normalize_occurrence_event
from llm_wiki.integration_events import session as session
from llm_wiki.integration_events import tool_payload as tool_payload
from llm_wiki.integration_lifecycle import after_breadcrumb as after_breadcrumb
from llm_wiki.integration_lifecycle import after_breadcrumb_safely as after_breadcrumb_safely
from llm_wiki.integration_lifecycle import breadcrumb_input as breadcrumb_input
from llm_wiki.integration_lifecycle import delegate_wrote_daily_log as delegate_wrote_daily_log
from llm_wiki.integration_lifecycle import ingest_event as ingest_event
from llm_wiki.integration_lifecycle import ingest_session_end as ingest_session_end
from llm_wiki.integration_lifecycle import publish_event_breadcrumb as publish_event_breadcrumb
from llm_wiki.integration_lifecycle import transcript_present as transcript_present
from llm_wiki.integration_session_start import append_context as append_context
from llm_wiki.integration_session_start import (
    recovered_project_handoff as recovered_project_handoff,
)
from llm_wiki.integration_session_start import run_maintenance_command as run_maintenance_command
from llm_wiki.integration_session_start import (
    run_session_start_maintenance as run_session_start_maintenance,
)
from llm_wiki.integration_session_start import (
    write_session_start_debug as write_session_start_debug,
)
from llm_wiki.integration_settings import CAPTURE_DELEGATES as CAPTURE_DELEGATES
from llm_wiki.integration_settings import CAPTURE_DRAIN_MAX_TASKS as CAPTURE_DRAIN_MAX_TASKS
from llm_wiki.integration_settings import CAPTURE_DRAIN_SECONDS as CAPTURE_DRAIN_SECONDS
from llm_wiki.integration_settings import DELEGATE_STARTUP_SECONDS as DELEGATE_STARTUP_SECONDS
from llm_wiki.integration_settings import DELEGATE_TIMEOUT_SECONDS as DELEGATE_TIMEOUT_SECONDS
from llm_wiki.integration_settings import DELEGATE_TIMEOUTS as DELEGATE_TIMEOUTS
from llm_wiki.integration_settings import DELEGATES as DELEGATES
from llm_wiki.integration_settings import HOST_HOOK_TIMEOUT_SECONDS as HOST_HOOK_TIMEOUT_SECONDS
from llm_wiki.integration_settings import INGESTED_DELEGATES as INGESTED_DELEGATES
from llm_wiki.integration_settings import (
    MAINTENANCE_DRAIN_TIMEOUT_SECONDS as MAINTENANCE_DRAIN_TIMEOUT_SECONDS,
)
from llm_wiki.integration_settings import MAX_CAPTURE_EVIDENCE_BYTES as MAX_CAPTURE_EVIDENCE_BYTES
from llm_wiki.integration_settings import MAX_STDIN_BYTES as MAX_STDIN_BYTES
from llm_wiki.integration_settings import MAX_TRANSCRIPT_TEXT_CHARS as MAX_TRANSCRIPT_TEXT_CHARS
from llm_wiki.integration_settings import SCRIPTS_DIR as SCRIPTS_DIR
from llm_wiki.integration_transcripts import capture_occurred_at as capture_occurred_at
from llm_wiki.integration_transcripts import capture_path_evidence as capture_path_evidence
from llm_wiki.integration_transcripts import capture_transcript_text as capture_transcript_text
from llm_wiki.integration_transcripts import (
    validated_capture_transcript_path as validated_capture_transcript_path,
)
from llm_wiki.integration_transcripts import whole_tokens_head as whole_tokens_head
from llm_wiki.integration_transcripts import whole_tokens_tail as whole_tokens_tail
from llm_wiki.integration_transient_files import (
    restrict_file_permissions as restrict_file_permissions,
)
from llm_wiki.integration_transient_files import (
    write_transient_transcript as write_transient_transcript,
)


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source")
    parser.add_argument("--event")
    parser.add_argument("--delegate")
    parser.add_argument("--checkpoint-type")
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--maintenance", action="store_true")
    parser.add_argument("--capture-worker", action="store_true")
    parser.add_argument("--running-capture", metavar="PAYLOAD_JSON")
    return parser


def _run_running_capture(args: argparse.Namespace) -> int:
    """The detached half of the twentieth-prompt capture; the payload rides in argv."""
    if not args.source:
        raise ValueError("invalid integration event")
    payload = json.loads(args.running_capture)
    if not isinstance(payload, dict):
        raise ValueError("invalid integration event")
    capture_running_session(args.source, payload)
    return 0


def run_active_capture_worker_once() -> int:
    from flush_memory import process_new_capture, run_capture_worker_once
    from markdown_transaction import active_markdown_coordinator
    from queue_core import (
        active_memory_queue,
    )

    vault = Path(runtime_roots.vault_root()).resolve(strict=True)
    state_root = Path(runtime_roots.state_root()).resolve(strict=True)
    queue = active_memory_queue(vault, state_root)
    coordinator = active_markdown_coordinator(vault, state_root)
    process_missing = partial(process_new_capture, queue, coordinator)
    work = partial(
        run_capture_worker_once, queue, coordinator, process_missing=process_missing,
        handler_versions=(1, 2),
        settled_failure=_record_settled_capture_failure,
    )
    drain_capture_work(work)
    return 0


def drain_capture_work(work) -> None:
    """Drain settled captures; named failures retain their existing retry policy."""
    deadline = time.monotonic() + CAPTURE_DRAIN_SECONDS
    for _ in range(CAPTURE_DRAIN_MAX_TASKS):
        if work() is None:
            return
        if time.monotonic() >= deadline:
            break
    # Every completed work() has released its owner. One successor checks for
    # any remainder, including intents whose event wake met our live owner.
    memory_state.spawn_detached(
        [sys.executable, str(SCRIPTS_DIR / "integration_adapter.py"), "--capture-worker"]
    )


def _record_settled_capture_failure(error: BaseException) -> None:
    """A failed task was settled by the queue; retain its actual diagnostic."""
    from capture_diagnostics import record_capture_failure
    from secret_redact import describe_error_chain

    record_capture_failure(
        "adapter_capture_worker", describe_error_chain(error), error=error
    )


def _oversize_stdin() -> ValueError:
    """Name the bound that refused, not the generic word for every refusal.

    `invalid integration event` named nothing, so a capture lost to a payload
    one byte over the limit read exactly like a malformed one.
    """
    return ValueError(
        f"integration event payload exceeds the {MAX_STDIN_BYTES}-byte adapter limit"
    )


def _decode_stdin(data: bytes) -> str:
    if len(data) > MAX_STDIN_BYTES:
        raise _oversize_stdin()
    return data.decode("utf-8")


def _read_stdin_bounded() -> str:
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    data = stream.read(MAX_STDIN_BYTES + 1)
    if isinstance(data, bytes):
        return _decode_stdin(data)
    if len(data.encode("utf-8")) > MAX_STDIN_BYTES:
        raise _oversize_stdin()
    return data


def read_hook_input() -> dict[str, Any]:
    text = _read_stdin_bounded()
    if not text:
        text = "{}"
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("invalid integration event")
    return raw


def _apply_checkpoint_arg(raw: dict[str, Any], checkpoint_type: str | None) -> dict[str, Any]:
    if not checkpoint_type:
        return raw
    projected = dict(raw)
    projected["checkpoint_type"] = checkpoint_type
    return projected


def _legacy_output(
    source: str, event_type: str, result: dict[str, Any]
) -> dict[str, object] | None:
    if event_type != "session_start":
        return None
    if source != "claude":
        return result
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": result.get("context", ""),
        }
    }


def _delegate_forwards_stdout(name: str) -> bool:
    return name in {
        "session_start_context.py",
        "session_start_project_state.py",
        "user_prompt_capture.py",
    }


def run_own_delegate(args: argparse.Namespace, envelope: EventEnvelope) -> None:
    """A delegate that is not this event's capture delegate speaks for itself."""
    project_checkpoint.observe_checkpoint_fail_open(envelope)
    integration_delegates.run_delegate(
        args.delegate,
        canonical_capture_payload(envelope),
        forward_stdout=_delegate_forwards_stdout(args.delegate),
    )


def dispatch_cli_event(
    args: argparse.Namespace, envelope: EventEnvelope | None
) -> dict[str, object] | None:
    if envelope is None:
        return None
    if args.delegate and not _is_capture_delegate(args.delegate, envelope.event_type):
        run_own_delegate(args, envelope)
        return None
    result = ingest_event(
        envelope, background=args.background,
        force_stub=args.delegate == "session_end_project_tag.py",
    )
    return _legacy_output(args.source, envelope.event_type, result)


def _is_capture_delegate(delegate: str, event_type: str) -> bool:
    return delegate == INGESTED_DELEGATES.get(event_type) or (
        event_type == "session_end" and delegate == "session_end_project_tag.py"
    )


# Set by `memory_state.spawn_detached` and by every provider call
# (`llm_client`): the process under this variable is the memory system itself.
REENTRY_MARKER = "CLAUDE_INVOKED_BY"


def is_memory_automation() -> bool:
    """True inside a process the memory system started.

    A host event raised by one of our own calls is our own traffic: capturing it
    would classify the memory system's prompt as a session, and on a host whose
    machine-managed settings register these hooks it would do so on every call.
    The two retired delegates had this guard; the adapter that replaced them did
    not. The worker and maintenance modes are not host events and never reach
    here. See
    `docs/research/2026-09-17-the-six-capture-corrections-the-first-round-left.md`.
    """
    return bool(os.environ.get(REENTRY_MARKER, "").strip())


def _require_named_event(args: argparse.Namespace) -> None:
    if not args.source or not args.event:
        raise ValueError("invalid integration event")


def run_cli_event(args: argparse.Namespace) -> dict[str, object] | None:
    _require_named_event(args)
    if is_memory_automation():
        return None
    raw = _apply_checkpoint_arg(read_hook_input(), args.checkpoint_type)
    envelope = normalize_occurrence_event(args.source, args.event, raw)
    return dispatch_cli_event(args, envelope)


def failed_operation(args: argparse.Namespace | None) -> str:
    """Name the invocation that failed, not the absence of an event.

    `--capture-worker` and `--maintenance` carry no `--event`, so the old
    `args.event or "unknown"` filed every failure of both under
    `adapter_unknown`. Twenty-two `intent_fence_lost` rows on this vault were
    read as publisher failures because of it, when only a worker can raise that
    string with no event attached. The process knows which of the three it is.
    """
    if args is None:
        return "unparsed"
    named = [flag for flag in CLI_MODES if getattr(args, flag, False)]
    return next(iter(named), getattr(args, "event", None) or "unknown")


# The invocations that carry no `--event`, in the order `main` tries them.
CLI_MODES = ("maintenance", "capture_worker", "running_capture")


def _cli_mode(args: argparse.Namespace):
    """The runner of the mode this invocation asked for, or None for a host event."""
    runners = {
        "maintenance": lambda _args: run_session_start_maintenance(),
        "capture_worker": lambda _args: run_active_capture_worker_once(),
        "running_capture": _run_running_capture,
    }
    named = [runners[flag] for flag in CLI_MODES if getattr(args, flag, False)]
    return next(iter(named), None)


def _skip_reason(error: BaseException) -> str:
    """The error's class, and its text only for our own refusals.

    A provider timeout carries the command it ran; the allowlist refusal
    carries a fixed sentence. The first must stay off stderr, the second is
    what the operator needs to read (issue #23).
    """
    name = type(error).__name__
    if isinstance(error, PermissionError):
        return f"{name}: {error}"[:MAX_SKIP_REASON_CHARS]
    return name


# A permission refusal printed to stderr when a capture is skipped (issue #23): one line an
# operator reads. A readability trade-off, not measured.
MAX_SKIP_REASON_CHARS = 240


def record_cli_capture_failure(
    args: argparse.Namespace | None, error: BaseException
) -> None:
    """Leave a durable trace of a capture this boundary swallowed.

    Everything below this point returns quietly so a hook never breaks the
    user's session, and for a long time that meant a failing capture was
    indistinguishable from a session with nothing to capture. Measured on this
    machine on 2026-08-26: every `session_end` raised
    `ReliabilityV3ValidationError: legacy_protocol_unquiesced` and printed
    `capture skipped`, so no session had been recorded since the 2026-08-24
    backfill and nothing anywhere said so. Recording is itself best effort —
    diagnostics must never become the reason a hook fails.
    """
    if isinstance(error, SystemExit):
        return
    try:
        from capture_diagnostics import record_capture_failure
        from secret_redact import describe_error_chain

        record_capture_failure(
            f"adapter_{failed_operation(args)}",
            describe_error_chain(error),
            error=error,
        )
    except Exception:  # noqa: BLE001 - a lost trace must not lose the session
        pass


def main(argv: Sequence[str] | None = None) -> int:
    """Host-safe CLI: invalid input and capture failures never escape."""
    output: dict[str, object] | None = None
    # `--help` and a malformed argv raise before `args` exists, and the handler
    # below reads it: bind it first so asking for help is not recorded as a
    # lost capture.
    args: argparse.Namespace | None = None
    try:
        args = parser().parse_args(argv)
        mode = _cli_mode(args)
        if mode is not None:
            return mode(args)
        output = run_cli_event(args)
    except (Exception, SystemExit) as error:  # noqa: BLE001
        record_cli_capture_failure(args, error)
        # The reason on the hook's own stderr, not only in the failure log:
        # issue #23 found "capture skipped" alone said nothing about the
        # transcript allowlist that refused the path.
        print(f"integration_adapter: capture skipped: {_skip_reason(error)}", file=sys.stderr)
        output = None
    if output is not None:
        print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
