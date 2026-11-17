"""Capturing precompact, session-end and breadcrumb events."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import project_checkpoint
from event_envelope import EventEnvelope
from llm_wiki import (
    event_project,
    integration_capture_records,
    integration_delegates,
    integration_session_start,
    runtime_roots,
)
from llm_wiki.integration_capture_payload import canonical_capture_payload
from llm_wiki.integration_capture_records import (
    _cleanup_transient_transcript,
    _lifecycle_metadata_text,
    _materialize_event_transcript,
    _record_capture_intent,
    _session_end_trigger,
    capture_source_record,
)
from llm_wiki.integration_session_start import (
    _ingest_result,
    _ingest_session_start,
)
from secret_redact import describe_error


def _capture_precompact(
    envelope: EventEnvelope,
    payload: dict[str, Any],
    slug: str | None,
    project_dir: Path | None,
    result: dict[str, Any],
    intent_id: str | None,
) -> bool:
    if not payload.get("transcript_path"):
        result["heartbeat_recorded"] = integration_session_start.record_activity(envelope, slug, project_dir)
        return False
    return integration_capture_records.wake_capture_worker(result, intent_id)


def _ingest_precompact(
    envelope: EventEnvelope,
    payload: dict[str, Any],
    slug: str | None,
    project_dir: Path | None,
    result: dict[str, Any],
    force_stub: bool,
    trigger: str | None,
) -> None:
    transient_path = _materialize_event_transcript(envelope, payload, result)
    intent_id = None
    try:
        intent_id = integration_capture_records.publish_durable_capture_intent(
            envelope, payload, slug, project_checkpoint.string(payload.get("trigger"))
        )
        _record_capture_intent(result, intent_id)
        _capture_precompact(
            envelope, payload, slug, project_dir, result, intent_id
        )
    finally:
        _cleanup_transient_transcript(transient_path)


def _decoded_delegate_report(stdout: object) -> dict[str, Any]:
    try:
        reported = json.loads(str(stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return {}
    return reported if isinstance(reported, dict) else {}


def _reported_daily_log(stdout: object) -> bool | None:
    """The delegate's own answer, or None when it did not give one."""
    written = _decoded_delegate_report(stdout).get("daily_log_written")
    if isinstance(written, bool):
        return written
    return None


def delegate_wrote_daily_log(tagged: object) -> bool:
    """What the tag delegate says it did, or its exit code when it says nothing.

    The delegate exits 0 whether it wrote a line or skipped the work (a session
    inside the vault, a session started in `$HOME`, no vault root), so the exit code
    alone reported tags that never happened. A delegate from an older install prints
    nothing and keeps the old reading. See
    `docs/research/2026-09-17-the-six-capture-corrections-the-first-round-left.md`.
    """
    exited_cleanly = getattr(tagged, "returncode", 0) == 0
    reported = _reported_daily_log(getattr(tagged, "stdout", ""))
    if reported is None:
        return exited_cleanly
    return exited_cleanly and reported


def _tag_session_end(
    payload: Mapping[str, Any], project_dir: Path | None, result: dict[str, Any]
) -> None:
    tagged = integration_delegates.run_delegate("session_end_project_tag.py", payload, project_dir=project_dir)
    result["daily_log_written"] = delegate_wrote_daily_log(tagged)
    result["returncode"] = getattr(tagged, "returncode", 0)


def _capture_session_end_without_transcript(
    envelope: EventEnvelope,
    payload: dict[str, Any],
    slug: str | None,
    project_dir: Path | None,
    result: dict[str, Any],
    force_stub: bool,
    intent_id: str | None = None,
) -> bool:
    """Replay a retained forced marker, otherwise leave only a heartbeat."""
    if force_stub:
        _tag_session_end(payload, project_dir, result)
        return integration_capture_records.wake_capture_worker(result, intent_id)
    if slug and project_dir:
        result["heartbeat_recorded"] = integration_session_start.record_activity(envelope, slug, project_dir)
    return False


def transcript_present(payload: Mapping[str, Any]) -> bool:
    """A path that names nothing is not a transcript; the session left no file.

    The branch used to turn on the path being *set*, so a session whose
    transcript had already gone took the reading route and died on
    `resolve(strict=True)`. Measured on this vault 2026-09-02: 27 of the 452
    recorded capture losses were that `FileNotFoundError`, and it was the only
    kind still happening — four of them that morning, all from sessions started
    outside any project, whose transcripts live under `-home-user` and `-tmp`.

    Nothing is recovered by crashing there: if the file is gone, its contents
    are gone with it. What changes is that the session is handled by the route
    written for exactly this case instead of being reported as a failed capture.
    """
    raw = payload.get("transcript_path")
    if not isinstance(raw, str) or not raw:
        return False
    try:
        return Path(raw).is_file()
    except OSError:
        return False


def _capture_session_end(
    envelope: EventEnvelope,
    payload: dict[str, Any],
    slug: str | None,
    project_dir: Path | None,
    result: dict[str, Any],
    force_stub: bool,
    intent_id: str | None,
) -> bool:
    if transcript_present(payload):
        _tag_session_end(payload, project_dir, result)
        return integration_capture_records.wake_capture_worker(result, intent_id)
    return _capture_session_end_without_transcript(
        envelope, payload, slug, project_dir, result, force_stub, intent_id
    )


def ingest_session_end(
    envelope: EventEnvelope,
    payload: dict[str, Any],
    slug: str | None,
    project_dir: Path | None,
    result: dict[str, Any],
    force_stub: bool,
    trigger: str | None,
) -> None:
    transient_path = _materialize_event_transcript(envelope, payload, result)
    payload["trigger"] = _session_end_trigger(trigger, payload)
    payload["force_session_marker"] = force_stub
    payload["capture_marker"] = capture_source_record(
        envelope, slug, project_checkpoint.string(payload.get("trigger")), _lifecycle_metadata_text(envelope)
    )
    intent_id = None
    try:
        intent_id = integration_capture_records.publish_durable_capture_intent(
            envelope, payload, slug, project_checkpoint.string(payload.get("trigger"))
        )
        _record_capture_intent(result, intent_id)
        _capture_session_end(
            envelope, payload, slug, project_dir, result, force_stub, intent_id
        )
    finally:
        _cleanup_transient_transcript(transient_path)


def breadcrumb_input(envelope: EventEnvelope) -> dict:
    data = envelope.to_dict()
    for field in ("event_id", "content_hash", "occurred_at", "captured_at"):
        data.pop(field)
    return data


def publish_event_breadcrumb(envelope: EventEnvelope):
    from breadcrumb_storage import publish_breadcrumb
    from markdown_transaction import active_markdown_coordinator
    from queue_core import (
        active_memory_queue,
    )

    vault, state_root = Path(runtime_roots.vault_root()).resolve(strict=True), Path(runtime_roots.state_root()).resolve(strict=True)
    queue = active_memory_queue(vault, state_root)
    coordinator = active_markdown_coordinator(vault, state_root)
    scope = {"host": envelope.agent, "session": envelope.session,
             "worktree": envelope.worktree, "kind": envelope.event_type,
             "occurrence": envelope.source_event_id or str(uuid.uuid4())}
    return publish_breadcrumb(
        queue, coordinator, scope, breadcrumb_input(envelope),
        occurred_at=envelope.occurred_at, accepted_at=envelope.captured_at,
        time_origin=envelope.payload.get("capture_time_origin", "acceptance"),
    )


def after_breadcrumb(envelope: EventEnvelope, payload: dict, result: dict) -> None:
    # The complete occurrence is durable. Its existing queue worker performs
    # the recoverable project transaction before completing the capture.
    slug, _project_dir = event_project.project_context(envelope)
    result["slug"] = slug
    if envelope.event_type == "user_prompt":
        from prompt_capture_followup import (
            after_prompt_capture,
        )

        after_prompt_capture(payload, slug)


def after_breadcrumb_safely(envelope: EventEnvelope, payload: dict, result: dict) -> None:
    try:
        after_breadcrumb(envelope, payload, result)
    except Exception as error:
        result["post_capture_error"] = describe_error(error)
        project_checkpoint.log_hook_error("accepted breadcrumb follow-up", result["post_capture_error"])


def _ingest_breadcrumb_event(envelope: EventEnvelope) -> dict:
    publication = publish_event_breadcrumb(envelope)
    payload = canonical_capture_payload(envelope)
    result = _ingest_result(None, payload)
    result.update(capture_durable=True, capture_registered=publication.registered,
                  capture_registration_error=publication.registration_error)
    _record_capture_intent(result, publication.intent_id)
    after_breadcrumb_safely(envelope, payload, result)
    integration_capture_records.wake_capture_worker(result, publication.intent_id)
    return result


def ingest_event(
    envelope: EventEnvelope,
    *,
    force_stub: bool = False,
    trigger: str | None = None,
    background: bool = False,
) -> dict[str, Any]:
    """Apply shared lifecycle persistence policy to a normalized envelope.

    `background` says the host runs this hook without waiting for it, so the tool
    breadcrumb may wait out a writer the way a foreground hook cannot.
    """
    if envelope.event_type in {"user_prompt", "post_tool_use"}:
        return _ingest_breadcrumb_event(envelope)
    project_checkpoint.observe_checkpoint_fail_open(envelope)
    payload = canonical_capture_payload(envelope)
    slug, project_dir = event_project.project_context(envelope)
    result = _ingest_result(slug, payload)
    handlers = {
        "session_start": _ingest_session_start,
        "pre_compact": _ingest_precompact,
        "session_end": ingest_session_end,
    }
    handler = handlers.get(envelope.event_type)
    if handler is not None:
        handler(envelope, payload, slug, project_dir, result, force_stub, trigger)
    return result

