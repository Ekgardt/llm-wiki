"""Queue validation; callers own time, admission and transactions."""
from __future__ import annotations

from collections.abc import Mapping

from llm_wiki.queue_contracts import (
    _MAX_RESULT_BYTES,
    _MAX_RUNTIME_ATTEMPTS,
    _STATES,
    QueueFailure,
    ResultConflictError,
)


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
