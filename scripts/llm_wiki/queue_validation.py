"""Queue validation; callers own time, admission and transactions."""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from typing import Literal

from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.formats.queue_payload import _MAX_QUEUE_PAYLOAD_BYTES
from llm_wiki.queue_contracts import (
    _MAX_RESULT_BYTES,
    _MAX_RUNTIME_ATTEMPTS,
    _STATES,
    QueueFailure,
    QueueOperationError,
    ResultConflictError,
)
from llm_wiki.restricted_paths import restricted_relative_path


def _require_task_kind(kind: object) -> None:
    if not isinstance(kind, str) or not kind:
        raise ValueError("kind must be a non-empty bounded string")
    if len(kind.encode("utf-8")) > 64:
        raise ValueError("kind must be a non-empty bounded string")

def _require_bounded_int(value: object, low: int, high: int, message: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(message)
    if not low <= value <= high:
        raise ValueError(message)

def _require_dedupe_key(dedupe_key: object) -> None:
    if dedupe_key is None:
        return
    if (
        not isinstance(dedupe_key, str)
        or not dedupe_key
        or len(dedupe_key.encode("utf-8")) > 512
    ):
        raise ValueError("dedupe_key must be a non-empty bounded string")

def _require_enqueue_arguments(
    kind: object,
    handler_version: object,
    priority: object,
    dedupe_key: object,
) -> None:
    _require_task_kind(kind)
    _require_bounded_int(
        handler_version,
        1,
        2_147_483_647,
        "handler_version must be a positive bounded integer",
    )
    _require_bounded_int(
        priority, -100, 100, "priority must be an integer from -100 to 100"
    )
    _require_dedupe_key(dedupe_key)

def _require_non_empty_string(value: object, message: str) -> None:
    """Refuse anything that is not a non-empty string."""
    if not isinstance(value, str) or not value:
        raise ValueError(message)

def _require_legacy_enqueue_arguments(
    kind: object, handler_version: object, priority: object, dedupe_key: object
) -> None:
    _require_non_empty_string(kind, "kind must be a non-empty string")
    _require_bounded_int(
        handler_version,
        1,
        2_147_483_647,
        "handler_version must be a positive integer",
    )
    _require_bounded_int(
        priority, -100, 100, "priority must be an integer from -100 to 100"
    )
    if dedupe_key is None:
        return
    _require_non_empty_string(dedupe_key, "dedupe_key must be a non-empty string")

def _validate_retry_policy(
    max_attempts: int, retry_base_seconds: int, retry_cap_seconds: int
) -> None:
    for name, value in (
        ("max_attempts", max_attempts),
        ("retry_base_seconds", retry_base_seconds),
        ("retry_cap_seconds", retry_cap_seconds),
    ):
        _check_policy_integer(name, value)
    if not 1 <= max_attempts <= _MAX_RUNTIME_ATTEMPTS:
        raise ValueError(
            f"max_attempts must be between 1 and {_MAX_RUNTIME_ATTEMPTS}"
        )
    _check_retry_window(retry_base_seconds, retry_cap_seconds)

def _check_policy_integer(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")

def _check_retry_bounds(retry_base_seconds: int, retry_cap_seconds: int) -> None:
    if retry_base_seconds <= 0:
        raise ValueError("retry_base_seconds must be positive")
    if retry_cap_seconds <= 0:
        raise ValueError("retry_cap_seconds must be positive")

def _check_retry_window(retry_base_seconds: int, retry_cap_seconds: int) -> None:
    """Both ends of the backoff window are positive, and it is a window."""
    _check_retry_bounds(retry_base_seconds, retry_cap_seconds)
    if retry_base_seconds > retry_cap_seconds:
        raise ValueError("retry_base_seconds must not exceed retry_cap_seconds")

def _validate_worker_policy(
    lease_seconds: int,
    heartbeat_seconds: int,
    max_attempts: int,
    retry_base_seconds: int,
    retry_cap_seconds: int,
) -> None:
    _validate_retry_policy(max_attempts, retry_base_seconds, retry_cap_seconds)
    for name, value in (
        ("lease_seconds", lease_seconds),
        ("heartbeat_seconds", heartbeat_seconds),
    ):
        if not _is_positive_int(value):
            raise ValueError(f"{name} must be a positive integer")
    if heartbeat_seconds >= lease_seconds:
        raise ValueError("heartbeat_seconds must be less than lease_seconds")

def _check_result_bytes(result: bytes) -> None:
    if not isinstance(result, bytes):
        raise TypeError("result must be bytes")
    if len(result) > _MAX_RESULT_BYTES:
        raise ValueError("result exceeds maximum queue result size")

def _check_result_request(operation_id: str, result: bytes) -> None:
    """What a caller has to hand over before anything is written."""
    if not operation_id:
        raise ValueError("operation_id must be non-empty")
    _check_result_bytes(result)

def _check_result_operation(row: Mapping[str, object], operation_id: str) -> None:
    """A lease publishes for exactly one operation."""
    existing = row["result_operation_id"]
    if existing is not None and existing != operation_id:
        raise ResultConflictError("lease already published a different operation")

def _require_error_code(failure: QueueFailure) -> None:
    if not failure.error_code:
        raise ValueError("error_code must be non-empty")

def _require_positive_task_lease(lease_seconds: int) -> None:
    if lease_seconds <= 0:
        raise ValueError("lease must be positive")

def _require_claim_owner_and_lease(owner: str, lease_seconds: int) -> None:
    if not owner:
        raise ValueError("owner must be non-empty")
    _require_positive_task_lease(lease_seconds)

def _is_positive_int(value: object) -> bool:
    """A real positive integer; `True` is not one."""
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    return value > 0

def _require_positive_lease(lease_seconds: object) -> None:
    """Refuse a fence lease that is not a positive number of seconds."""
    if not _is_positive_int(lease_seconds):
        raise ValueError("source fence lease must be a positive integer")

def _valid_heartbeat_interval(heartbeat_seconds: object, lease_seconds: object) -> bool:
    """A heartbeat interval that is strictly shorter than the lease it renews."""
    if not _is_positive_int(heartbeat_seconds):
        return False
    if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool):
        return False
    return lease_seconds > heartbeat_seconds

def _require_known_states(states: tuple[str, ...]) -> None:
    """Refuse a state filter that names something the queue cannot hold."""
    if not states or any(state not in _STATES for state in states):
        raise ValueError("invalid queue state")


# Existing capture wire labels, retained without new policy.
_CAPTURE_TERMINAL_DISPOSITION_FIELDS = {
    "markdown_committed": {
        "kind",
        "transaction_id",
        "operation_id",
        "decision_sha256",
        "outputs",
    },
    "no_durable_content": {"kind", "decision_sha256"},
}

_CONSUMER_KINDS = frozenset({"transaction", "terminal", "corrupt-disposition"})


def _require_capture_intent_path(value: str, state: Literal["pending", "ready"]) -> None:
    restricted_relative_path(value, ("run/capture-intents",))
    if len(value.encode("utf-8")) > 4096:
        raise ValueError(f"{state} capture intent path is invalid")
    pattern = rf"run/capture-intents/{state}/[0-9a-f]{{2}}/[0-9a-f]{{64}}\.json"
    if re.fullmatch(pattern, value) is None:
        raise ValueError(f"{state} capture intent path is invalid")

def _require_lower_sha256(value: str, label: str) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{label} must be lowercase 64-hex")

def _require_capture_intent_size(value: int) -> None:
    if type(value) is not int:
        raise ValueError("capture intent byte size is invalid")
    if not 1 <= value <= _MAX_QUEUE_PAYLOAD_BYTES:
        raise ValueError("capture intent byte size is invalid")

def _require_capture_intent_descriptor(
    intent_id: str,
    pending_path: str,
    intent_sha256: str,
    byte_size: int,
) -> None:
    _require_lower_sha256(intent_id, "intent_id")
    _require_lower_sha256(intent_sha256, "intent_sha256")
    _require_capture_intent_size(byte_size)
    _require_capture_intent_path(pending_path, "pending")
    expected = f"run/capture-intents/pending/{intent_id[:2]}/{intent_id}.json"
    if pending_path != expected:
        raise ValueError("capture intent descriptor is invalid")

def _require_capture_decision_path(value: str, intent_id: str, stage: str) -> None:
    restricted_relative_path(value, ("run/queue-results",))
    expected = _capture_decision_relative_path(intent_id, stage)
    if value != expected:
        raise ValueError("decision path is invalid")

def _capture_decision_relative_path(intent_id: str, stage: str) -> str:
    key = sha256_bytes(canonical_json_bytes({"intent_id": intent_id, "stage": stage}))
    return f"run/queue-results/capture-decision-{key}.json"

def _require_capture_terminal_path(value: str, intent_id: str) -> None:
    restricted_relative_path(value, ("run/queue-results",))
    expected = f"run/queue-results/capture-{intent_id}.json"
    if value != expected:
        raise ValueError("capture terminal path is invalid")

def _capture_terminal_disposition_fields(kind: object) -> set[str] | None:
    if not isinstance(kind, str):
        return None
    return _CAPTURE_TERMINAL_DISPOSITION_FIELDS.get(kind)

def _require_bounded_intent_path(intent_path: object) -> None:
    if not isinstance(intent_path, str) or len(intent_path.encode("utf-8")) > 4096:
        raise ValueError("intent_path is invalid")

def _require_capture_identity(
    intent_id: object, intent_sha256: object, intent_path: object
) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", str(intent_id)) is None:
        raise ValueError("intent_id must be lowercase 64-hex")
    if re.fullmatch(r"[0-9a-f]{64}", str(intent_sha256)) is None:
        raise ValueError("intent_sha256 must be lowercase 64-hex")
    _require_bounded_intent_path(intent_path)

def _require_capture_fence(
    capture_fence: object, intent_id: str, owner: object, fence_type: type
) -> None:
    if (
        not isinstance(capture_fence, fence_type)
        or capture_fence.intent_id != intent_id
        or capture_fence.mode != "capture"
        or capture_fence.owner != owner
    ):
        raise ValueError("capture fence does not match the intent and owner")

def _require_semantic_decision_shape(stage: str, decision_sha256: str) -> None:
    if stage not in {"flush", "feedback", "feedback-verify"}:
        raise ValueError("semantic decision stage is invalid")
    if re.fullmatch(r"[0-9a-f]{64}", str(decision_sha256)) is None:
        raise ValueError("decision_sha256 must be lowercase 64-hex")

def _require_worker_intent_fence(
    intent_fence: object, intent_id: str, fence_type: type
) -> None:
    if not isinstance(intent_fence, fence_type):
        raise ValueError("intent fence does not match")
    if intent_fence.intent_id != intent_id or intent_fence.mode != "worker":
        raise ValueError("intent fence does not match")

def _require_matching_decision_owner(
    owner: object, task_fence: object, intent_fence: object, owner_type: type
) -> None:
    if not isinstance(owner, owner_type):
        raise ValueError("semantic decision owner does not match fences")
    if task_fence.owner != owner or intent_fence.owner != owner:
        raise ValueError("semantic decision owner does not match fences")

def _require_repair_owner(owner: object, owner_type: type, message: str) -> None:
    if not isinstance(owner, owner_type) or owner.role != "repair":
        raise ValueError(message)

def _require_task_identifier(task_id: object) -> None:
    if not isinstance(task_id, str):
        raise ValueError("task ID is invalid")
    if not 1 <= len(task_id.encode("utf-8")) <= 256:
        raise ValueError("task ID is invalid")

def _check_quarantine_names(task_id: str, reason: str) -> None:
    """A quarantine names one task and says why, both within bounds."""
    if not isinstance(task_id, str) or not 1 <= len(task_id.encode("utf-8")) <= 256:
        raise ValueError("task ID is invalid")
    if not isinstance(reason, str) or not 1 <= len(reason.encode("utf-8")) <= 4096:
        raise ValueError("quarantine reason is invalid")

def _check_capture_intent_relative_path(intent_path: object) -> None:
    """A published intent lives under the capture-intent directory, bounded."""
    if (
        not isinstance(intent_path, str)
        or "\\" in intent_path
        or not intent_path.startswith("run/capture-intents/")
        or len(intent_path.encode("utf-8")) > 4096
    ):
        raise ValueError("intent_path is invalid")

def _check_capture_intent_size(byte_size: object) -> None:
    if isinstance(byte_size, bool) or not isinstance(byte_size, int):
        raise ValueError("intent byte size is invalid")
    if not 1 <= byte_size <= _MAX_QUEUE_PAYLOAD_BYTES:
        raise ValueError("intent byte size is invalid")

def _capture_scan_cursor(after: tuple[str, str] | None) -> tuple[str, str]:
    if after is None:
        return "", ""
    if not isinstance(after, tuple) or len(after) != 2:
        raise ValueError("capture scan cursor must contain timestamp and identity")
    return _capture_cursor_text(after[0]), _capture_cursor_text(after[1])

def _capture_cursor_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("capture scan cursor fields must be text")
    return value

def _check_capture_intent_descriptor(
    intent_id: str, intent_path: str, intent_sha256: str, byte_size: int
) -> None:
    """Everything a publication has to name, before anything is written."""
    _check_capture_intent_digest(intent_id, "intent_id")
    _check_capture_intent_digest(intent_sha256, "intent_sha256")
    _check_capture_intent_relative_path(intent_path)
    _check_capture_intent_size(byte_size)

def _check_ready_intent_identity(
    existing: sqlite3.Row, intent_path: str, intent_sha256: str, byte_size: int
) -> None:
    """A republication has to name exactly the intent already recorded ready."""
    stored = (
        existing["relative_path"],
        existing["intent_sha256"],
        existing["byte_size"],
        existing["publication_state"],
    )
    if stored != (intent_path, intent_sha256, byte_size, "ready"):
        raise QueueOperationError("capture_intent_conflict")

def _check_resolution_reason(reason: object) -> None:
    if not isinstance(reason, str) or not 1 <= len(reason.encode("utf-8")) <= 4096:
        raise ValueError("resolution reason is invalid")

def _check_consumer_kind(consumer_kind: str) -> None:
    if consumer_kind not in _CONSUMER_KINDS:
        raise ValueError("consumer kind is invalid")

def _check_consumer_id(consumer_id: object) -> None:
    if not isinstance(consumer_id, str):
        raise ValueError("consumer ID is invalid")
    if not 1 <= len(consumer_id.encode("utf-8")) <= 4096:
        raise ValueError("consumer ID is invalid")

# These historical entry points have identical validation semantics.
_check_capture_intent_digest = _require_lower_sha256
