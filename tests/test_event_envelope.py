from __future__ import annotations

import hashlib
import json
import time
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest
from event_envelope import EventEnvelope, build_event_envelope

FIXED_TIME = datetime(2026, 7, 13, 12, 30, 45, tzinfo=timezone.utc)


def _prompt_event(**overrides) -> EventEnvelope:
    values = {
        "event_type": "user_prompt",
        "payload": {"prompt": "Refactor the auth module"},
        "occurred_at": FIXED_TIME,
        "captured_at": FIXED_TIME,
    }
    values.update(overrides)
    return build_event_envelope(**values)


def test_event_id_is_deterministic_for_canonically_equivalent_content():
    first = _prompt_event(payload={"prompt": "Refactor the auth module", "metadata": {"b": 2, "a": 1}})
    second = _prompt_event(payload={"metadata": {"a": 1, "b": 2}, "prompt": "Refactor the auth module"})

    assert first.event_id == second.event_id
    assert len(first.event_id) == 64


def test_event_id_ignores_capture_retry_time():
    later = datetime(2026, 7, 13, 12, 31, 45, tzinfo=timezone.utc)

    first = _prompt_event(captured_at=FIXED_TIME)
    retry = _prompt_event(captured_at=later)

    assert first.captured_at != retry.captured_at
    assert first.event_id == retry.event_id


def test_event_id_is_stable_when_source_time_is_missing():
    first = build_event_envelope(
        event_type="user_prompt",
        payload={"prompt": "same retry"},
        session="session-1",
    )
    time.sleep(0.002)
    retry = build_event_envelope(
        event_type="user_prompt",
        payload={"prompt": "same retry"},
        session="session-1",
        captured_at=FIXED_TIME,
    )

    assert first.occurred_at != retry.occurred_at
    assert first.event_id == retry.event_id


def test_explicit_source_event_id_separates_otherwise_identical_events():
    first = _prompt_event(source_event_id="host-event-1")
    second = _prompt_event(source_event_id="host-event-2")

    assert first.source_event_id == "host-event-1"
    assert first.event_id != second.event_id


def test_timestamps_are_serialized_as_timezone_aware_utc():
    offset_time = datetime.fromisoformat("2026-07-13T14:30:45+02:00")

    event = _prompt_event(occurred_at=offset_time, captured_at=offset_time)

    assert event.occurred_at.tzinfo is timezone.utc
    assert event.captured_at.tzinfo is timezone.utc
    assert event.to_dict()["occurred_at"] == "2026-07-13T12:30:45Z"
    assert event.to_dict()["captured_at"] == "2026-07-13T12:30:45Z"


def test_content_hash_is_full_sha256_of_canonical_redacted_payload():
    redacted_payload = {"prompt": "token=[REDACTED]", "metadata": {"b": 2, "a": 1}}

    event = _prompt_event(payload=redacted_payload)

    canonical = json.dumps(redacted_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert event.content_hash == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert len(event.content_hash) == 64


def test_missing_optional_source_fields_remain_null():
    serialized = _prompt_event().to_dict()

    assert serialized["agent"] is None
    assert serialized["session"] is None
    assert serialized["project"] is None
    assert serialized["worktree"] is None
    assert serialized["severity"] is None
    assert serialized["parent_event_id"] is None


@pytest.mark.parametrize(
    "field",
    [
        "agent",
        "session",
        "project",
        "worktree",
        "severity",
        "parent_event_id",
        "source_event_id",
    ],
)
def test_optional_source_fields_reject_non_string_values(field):
    with pytest.raises(ValueError, match="source fields must be strings or null"):
        _prompt_event(**{field: []})


def test_envelope_and_nested_payload_are_immutable():
    event = _prompt_event(payload={"prompt": "Refactor auth", "metadata": {"tags": ["security"]}})

    with pytest.raises(FrozenInstanceError):
        event.event_type = "post_tool_use"
    with pytest.raises(TypeError):
        event.payload["prompt"] = "changed"
    with pytest.raises(TypeError):
        event.payload["metadata"]["tags"][0] = "changed"


def test_payload_is_redacted_before_hashing_and_storage():
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"

    event = _prompt_event(
        payload={"prompt": f"Authorization: Bearer {secret}"},
        redact=lambda value: value.replace(secret, "[REDACTED]"),
    )

    serialized = event.to_dict()
    assert secret not in serialized["payload"]["prompt"]
    canonical = json.dumps(serialized["payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert event.content_hash == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    "field",
    [
        "agent",
        "session",
        "project",
        "worktree",
        "severity",
        "parent_event_id",
        "source_event_id",
    ],
)
def test_source_fields_are_redacted_inside_constructor(field):
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"

    event = _prompt_event(
        **{field: secret},
        redact=lambda value: value.replace(secret, "[REDACTED]"),
    )

    assert event.to_dict()[field] == "[REDACTED]"
    assert secret not in event.to_json()


def test_payload_string_keys_are_redacted_recursively():
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"

    event = _prompt_event(
        payload={"prompt": "safe", "metadata": {secret: "value"}},
        redact=lambda value: value.replace(secret, "[REDACTED]"),
    )

    assert event.to_dict()["payload"]["metadata"] == {"[REDACTED]": "value"}


def test_payload_rejects_keys_that_collide_after_redaction():
    def redact(value):
        return "[REDACTED]" if value.startswith("secret-") else value

    with pytest.raises(ValueError, match="payload keys collide after redaction"):
        _prompt_event(
            payload={"prompt": "safe", "metadata": {"secret-a": 1, "secret-b": 2}},
            redact=redact,
        )


def test_payload_revalidates_required_keys_after_redaction():
    secret = "secret-required-key"

    with pytest.raises(ValueError) as exc_info:
        _prompt_event(
            payload={"prompt": "safe"},
            redact=lambda value: secret if value == "prompt" else value,
        )

    assert str(exc_info.value) == "invalid event payload"
    assert secret not in str(exc_info.value)


def test_payload_revalidates_required_value_types_after_redaction():
    secret = "secret-required-value"

    with pytest.raises(ValueError) as exc_info:
        _prompt_event(
            payload={"prompt": secret},
            redact=lambda value: 123 if value == secret else value,
        )

    assert str(exc_info.value) == "invalid event payload"
    assert secret not in str(exc_info.value)


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": "safe", 1: "invalid"},
        {"prompt": "safe", "metadata": {1: "invalid"}},
    ],
)
def test_payload_rejects_non_string_mapping_keys(payload):
    with pytest.raises(ValueError, match="payload mapping keys must be strings"):
        _prompt_event(payload=payload)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_payload_rejects_non_finite_json_numbers(value):
    with pytest.raises(ValueError, match="payload must contain strict JSON values"):
        _prompt_event(payload={"prompt": "safe", "value": value})


@pytest.mark.parametrize("event_type", ["", "unknown", "UserPromptSubmit"])
def test_invalid_event_types_are_rejected(event_type):
    with pytest.raises(ValueError, match="event type"):
        _prompt_event(event_type=event_type)


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("user_prompt", {}),
        ("user_prompt", {"prompt": 123}),
        ("post_tool_use", {"tool_name": "Edit"}),
        ("post_tool_use", {"tool_name": 123, "target": "src/auth.py"}),
    ],
)
def test_invalid_typed_payloads_are_rejected(event_type, payload):
    with pytest.raises(ValueError, match="payload"):
        build_event_envelope(
            event_type=event_type,
            payload=payload,
            occurred_at=FIXED_TIME,
            captured_at=FIXED_TIME,
        )
