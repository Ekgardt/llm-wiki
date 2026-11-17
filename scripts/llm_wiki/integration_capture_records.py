"""Building and publishing the durable capture record of a session."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import capture_publication
import memory_state
import project_checkpoint
from event_envelope import EventEnvelope
from llm_wiki import event_project, runtime_roots
from llm_wiki.integration_capture_payload import canonical_capture_payload
from llm_wiki.integration_events import normalize_event
from llm_wiki.integration_session_start import _cleanup_runtime_transient
from llm_wiki.integration_settings import MAX_CAPTURE_EVIDENCE_BYTES, SCRIPTS_DIR
from llm_wiki.integration_transcripts import (
    _capture_evidence_text,
    _capture_nullable_text,
    capture_occurred_at,
)
from llm_wiki.integration_transient_files import write_transient_transcript
from memory_state import MAX_CAPTURE_INTENT_BYTES
from secret_redact import redact_secrets


def capture_source_record(
    envelope: EventEnvelope,
    slug: str | None,
    trigger: str | None,
    text: str,
) -> dict[str, object]:
    evidence = [{"role": "transcript", "parts": [{"type": "text", "text": text}]}]
    return {
        "source_occurrence_id": envelope.event_id,
        "source_event_id": envelope.source_event_id or envelope.event_id,
        "occurred_at": capture_occurred_at(envelope),
        "host": envelope.agent or "unknown",
        "event": envelope.event_type,
        "session": _capture_nullable_text(envelope.session, "capture session"),
        "project_slug": _capture_nullable_text(slug, "capture project slug"),
        "worktree": _capture_nullable_text(envelope.worktree, "capture worktree"),
        "trigger": _capture_nullable_text(trigger, "capture trigger"),
        "checkpoint_reason": _capture_nullable_text(
            envelope.payload.get("reason"), "capture checkpoint reason"
        ),
        "chunk_index": 0,
        "chunk_count": 1,
        "evidence": evidence,
    }


def encoded_capture_record(source: Mapping[str, object]) -> tuple[dict[str, object], bytes]:
    from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
    from llm_wiki.formats.schema import validate_schema

    evidence = source["evidence"]
    complete_digest = sha256_bytes(canonical_json_bytes(dict(source)))
    chunk_digest = sha256_bytes(canonical_json_bytes(evidence))
    identity = {
        "schema_version": "capture-intent/v1",
        "source_occurrence_id": source["source_occurrence_id"],
        "source_event_id": source["source_event_id"],
        "occurred_at": source["occurred_at"],
        "checkpoint_reason": source["checkpoint_reason"],
        "chunk_index": source["chunk_index"],
        "chunk_sha256": chunk_digest,
    }
    intent_id = sha256_bytes(canonical_json_bytes(identity))
    record = {
        "schema_version": "capture-intent/v1",
        "intent_id": intent_id,
        **dict(source),
        "complete_input_sha256": complete_digest,
        "chunk_sha256": chunk_digest,
    }
    validate_schema(record, SCRIPTS_DIR / "schemas" / "capture-intent-v1.json")
    return record, canonical_json_bytes(record)


# Times the capture record is shrunk to fit its byte bound before it is refused. basis unknown — value predates measurement; review when a capture is refused as unfittable.
CAPTURE_FIT_ATTEMPTS = 4


def _smaller_evidence_limit(limit: int, encoded_size: int) -> int:
    """The bound scaled by the growth just measured, less a tenth."""
    return int(limit * MAX_CAPTURE_INTENT_BYTES / encoded_size * 0.9)


def _fitting_capture_record(
    envelope: EventEnvelope,
    payload: Mapping[str, Any],
    slug: str | None,
    trigger: str | None,
) -> tuple[dict[str, object], bytes] | None:
    """The record and its bytes, with evidence cut until the encoded record fits.

    Quotes, backslashes and line breaks double inside a JSON string, so a bound on
    the raw text never bounded the record. See
    `docs/research/2026-09-17-the-evidence-fits-its-record-and-says-what-it-dropped.md`.
    """
    limit = MAX_CAPTURE_EVIDENCE_BYTES
    for _ in range(CAPTURE_FIT_ATTEMPTS):
        text = _capture_evidence_text(envelope, payload, limit)
        if text is None:
            return _marker_only_capture_record(envelope, payload, slug, trigger)
        record, encoded = encoded_capture_record(
            capture_source_record(envelope, slug, trigger, text)
        )
        if len(encoded) <= MAX_CAPTURE_INTENT_BYTES:
            return record, encoded
        limit = _smaller_evidence_limit(limit, len(encoded))
    raise ValueError("capture intent exceeds its byte limit")


def _lifecycle_metadata_text(envelope: EventEnvelope) -> str:
    metadata = {"event": envelope.event_type, "session": envelope.session,
                "source_occurrence_id": envelope.event_id,
                "occurred_at": capture_occurred_at(envelope),
                "transcript": "No transcript was supplied or available."}
    return json.dumps(metadata, sort_keys=True, ensure_ascii=False)


def _marker_only_capture_record(envelope, payload, slug, trigger):
    from session_end_project_tag import session_marker_plan

    if envelope.event_type != "session_end" or payload.get("force_session_marker") is not True:
        return None
    source = capture_source_record(envelope, slug, trigger, _lifecycle_metadata_text(envelope))
    return _encoded_marker_only_source(source, session_marker_plan(source, runtime_roots.vault_root()))


def _encoded_marker_only_source(source: dict, plan: object):
    if plan is None:
        return None
    source["evidence"][0]["role"] = "lifecycle"
    record, encoded = encoded_capture_record(source)
    if len(encoded) > MAX_CAPTURE_INTENT_BYTES:
        raise ValueError("lifecycle metadata exceeds the capture intent byte limit")
    return record, encoded


def publish_durable_capture_intent(
    envelope: EventEnvelope,
    payload: Mapping[str, Any],
    slug: str | None,
    trigger: str | None,
) -> str | None:
    from llm_wiki.formats.canonical import sha256_bytes
    from markdown_transaction import active_markdown_coordinator
    from queue_core import (
        active_memory_queue,
    )

    fitted = _fitting_capture_record(envelope, payload, slug, trigger)
    if fitted is None:
        return None
    record, encoded = fitted
    intent_id = str(record["intent_id"])
    intent_sha256 = sha256_bytes(encoded)
    pending_relative, ready_relative = capture_publication.capture_relative_paths(intent_id)
    state_root = Path(runtime_roots.state_root()).resolve(strict=True)
    capture_publication.ensure_capture_intent_directories(state_root, intent_id)
    queue = active_memory_queue(Path(runtime_roots.vault_root()), state_root)
    coordinator = active_markdown_coordinator(Path(runtime_roots.vault_root()), state_root)
    capture_publication.publish_capture_files_and_task(
        queue,
        coordinator,
        intent_id=intent_id,
        payload=encoded,
        intent_sha256=intent_sha256,
        pending_relative=pending_relative,
        ready_relative=ready_relative,
    )
    return intent_id


def _fallback_trigger(event_type: str, payload: Mapping[str, Any]) -> str | None:
    if event_type != "session_end":
        return project_checkpoint.string(payload.get("trigger"))
    return project_checkpoint.string(_session_end_trigger(project_checkpoint.string(payload.get("trigger")), payload))


def publish_capture_intent_from_payload(
    source: str, event_type: str, payload: Mapping[str, Any]
) -> str | None:
    """Durable fallback for the thin lifecycle hooks; never raises.

    The hooks hand the heavy work to a detached process. When that spawn fails,
    and the hook was invoked directly rather than through this adapter, nothing
    else has published an intent and the session is lost with it. Publishing the
    intent here keeps the work: the queue replays the flush at the next session,
    which is what "no user action required" has to mean on the failure path too.
    """
    try:
        return _publish_intent_from_payload(source, event_type, payload)
    except Exception:  # noqa: BLE001
        return None


def _publish_intent_from_payload(
    source: str, event_type: str, payload: Mapping[str, Any]
) -> str | None:
    envelope = normalize_event(source, event_type, dict(payload))
    canonical = canonical_capture_payload(envelope)
    slug, _project_dir = event_project.project_context(envelope)
    trigger = _fallback_trigger(event_type, canonical)
    return publish_durable_capture_intent(envelope, canonical, slug, trigger)


def capture_running_session(source: str, payload: Mapping[str, Any]) -> str | None:
    """Capture a session that is still running: publish its intent, wake the worker.

    The same record a compaction leaves, without the project checkpoint a real
    compaction writes. It raises, so the mode that runs it records the reason. See
    `docs/research/2026-09-17-the-twentieth-prompt-captures-the-session.md`.
    """
    intent_id = _publish_intent_from_payload(source, "pre_compact", payload)
    wake_capture_worker({}, intent_id)
    return intent_id


def _record_capture_intent(result: dict[str, Any], intent_id: str | None) -> None:
    if intent_id is not None:
        result["capture_intent_ids"] = [intent_id]


def _materialize_event_transcript(
    envelope: EventEnvelope,
    payload: dict[str, Any],
    result: dict[str, Any],
) -> Path | None:
    text = envelope.payload.get("transcript_text")
    if not isinstance(text, str):
        return None
    if not text:
        return None
    path = write_transient_transcript(envelope, text)
    payload["transcript_path"] = str(path)
    payload["ephemeral_transcript"] = True
    result["transcript_path"] = str(path)
    return path


def _cleanup_transient_transcript(path: Path | None) -> None:
    """The copy ends with its event, published or not (audit C-5).

    Nothing reads a kept copy since the flush command line was retired; see
    `docs/research/2026-09-25-a-transient-transcript-never-outlives-its-event.md`.
    """
    if path is None:
        return
    _cleanup_runtime_transient(path)


def wake_capture_worker(result: dict[str, Any], intent_id: str | None) -> bool:
    if intent_id is None:
        return False
    try:
        process_id = memory_state.spawn_detached(
            [
                sys.executable,
                str(SCRIPTS_DIR / "integration_adapter.py"),
                "--capture-worker",
            ]
        )
    except Exception:  # noqa: BLE001
        process_id = None
    started = process_id is not None
    result["flush_spawned"] = started
    return started


def _session_end_trigger(trigger: str | None, payload: Mapping[str, Any]) -> Any:
    if isinstance(trigger, str):
        safe_trigger = redact_secrets(trigger)
        if safe_trigger:
            return safe_trigger
    return payload.get("reason")

