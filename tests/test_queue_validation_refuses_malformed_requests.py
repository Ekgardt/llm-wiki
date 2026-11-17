from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import pytest
from llm_wiki import queue_validation
from llm_wiki.queue_contracts import MAX_RESULT_BYTES, QueueFailure

DIGEST = "a" * 64


@dataclass(frozen=True)
class IntentFence:
    intent_id: str
    mode: str
    owner: object = None


@dataclass(frozen=True)
class TaskFence:
    owner: object


@dataclass(frozen=True)
class DecisionOwner:
    name: str


def _refusals() -> list[tuple[Callable[[], object], str]]:
    owner = DecisionOwner("worker-a")
    other = DecisionOwner("worker-b")
    return [
        (
            lambda: queue_validation.require_legacy_enqueue_arguments("", 1, 0, None),
            "kind must be a non-empty string",
        ),
        (
            lambda: queue_validation.require_legacy_enqueue_arguments("capture", 1, 0, ""),
            "dedupe_key must be a non-empty string",
        ),
        (
            lambda: queue_validation.validate_retry_policy(3, cast(int, "5"), 10),
            "retry_base_seconds must be an integer",
        ),
        (
            lambda: queue_validation.validate_retry_policy(True, 5, 10),
            "max_attempts must be an integer",
        ),
        (
            lambda: queue_validation.check_result_bytes(b"x" * (MAX_RESULT_BYTES + 1)),
            "result exceeds maximum queue result size",
        ),
        (
            lambda: queue_validation.require_error_code(QueueFailure("")),
            "error_code must be non-empty",
        ),
        (lambda: queue_validation.require_positive_task_lease(0), "lease must be positive"),
        (lambda: queue_validation.require_claim_owner_and_lease("", 30), "owner must be non-empty"),
        (
            lambda: queue_validation.require_claim_owner_and_lease("worker", -1),
            "lease must be positive",
        ),
        (
            lambda: queue_validation.require_positive_lease(True),
            "source fence lease must be a positive integer",
        ),
        (lambda: queue_validation.require_known_states(()), "invalid queue state"),
        (lambda: queue_validation.require_known_states(("ready", "lost")), "invalid queue state"),
        (
            lambda: queue_validation.require_capture_identity("A" * 64, DIGEST, "p"),
            "intent_id must be lowercase 64-hex",
        ),
        (
            lambda: queue_validation.require_capture_identity(DIGEST, "short", "p"),
            "intent_sha256 must be lowercase 64-hex",
        ),
        (
            lambda: queue_validation.require_worker_intent_fence(
                IntentFence(DIGEST, "capture"), DIGEST, IntentFence
            ),
            "intent fence does not match",
        ),
        (
            lambda: queue_validation.require_worker_intent_fence(
                IntentFence("b" * 64, "worker"), DIGEST, IntentFence
            ),
            "intent fence does not match",
        ),
        (
            lambda: queue_validation.require_matching_decision_owner(
                "worker-a", TaskFence(owner), IntentFence(DIGEST, "worker", owner), DecisionOwner
            ),
            "semantic decision owner does not match fences",
        ),
        (
            lambda: queue_validation.require_matching_decision_owner(
                owner, TaskFence(owner), IntentFence(DIGEST, "worker", other), DecisionOwner
            ),
            "semantic decision owner does not match fences",
        ),
        (lambda: queue_validation.check_quarantine_names("", "reason"), "task ID is invalid"),
        (
            lambda: queue_validation.check_quarantine_names("task-1", ""),
            "quarantine reason is invalid",
        ),
        (
            lambda: queue_validation.check_capture_intent_descriptor(
                DIGEST, "run\\capture-intents\\x.json", DIGEST, 10
            ),
            "intent_path is invalid",
        ),
        (
            lambda: queue_validation.check_capture_intent_descriptor(
                DIGEST, "run/elsewhere/x.json", DIGEST, 10
            ),
            "intent_path is invalid",
        ),
        (
            lambda: queue_validation.check_resolution_reason("r" * 4097),
            "resolution reason is invalid",
        ),
        (lambda: queue_validation.check_consumer_id(7), "consumer ID is invalid"),
        (lambda: queue_validation.check_consumer_id(""), "consumer ID is invalid"),
    ]


@pytest.mark.parametrize(("request_call", "message"), _refusals())
def test_a_malformed_queue_request_is_refused_by_its_named_reason(
    request_call: Callable[[], object], message: str
) -> None:
    with pytest.raises(ValueError) as refused:
        request_call()

    assert str(refused.value) == message


def test_a_ready_capture_intent_path_too_long_for_the_queue_is_refused() -> None:
    long_path = "run/capture-intents/ready/" + "a/" * 2100 + "x.json"

    with pytest.raises(ValueError, match="ready capture intent path is invalid"):
        queue_validation.require_capture_intent_path(long_path, "ready")


def test_a_well_formed_ready_capture_intent_path_is_accepted() -> None:
    path = f"run/capture-intents/ready/{DIGEST[:2]}/{DIGEST}.json"

    queue_validation.require_capture_intent_path(path, "ready")


@pytest.mark.parametrize(
    ("heartbeat", "lease", "valid"),
    [
        (0, 30, False),
        (True, 30, False),
        (10, "30", False),
        (10, True, False),
        (30, 30, False),
        (10, 30, True),
    ],
)
def test_a_heartbeat_is_valid_only_when_strictly_shorter_than_an_integer_lease(
    heartbeat: object, lease: object, valid: bool
) -> None:
    assert queue_validation.valid_heartbeat_interval(heartbeat, lease) is valid


def test_valid_names_and_reasons_pass_every_bounded_check() -> None:
    queue_validation.require_legacy_enqueue_arguments("capture", 1, 0, "dedupe")
    queue_validation.check_quarantine_names("task-1", "corrupt payload")
    queue_validation.check_resolution_reason("operator discarded")
    queue_validation.check_consumer_id("consumer-1")
    queue_validation.require_known_states(("ready", "dead"))
    queue_validation.require_claim_owner_and_lease("worker", 30)
