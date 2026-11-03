"""A failed attempt carries the code of what failed, not one word for everything.

On the live vault 225 failed attempts of 25 dead `flush` tasks all said
`processor_failed`; the reason existed only in the failure trail, and only for a few.
See `docs/research/2026-09-23-a-dead-task-names-its-reason.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import flush_memory  # noqa: E402
import memory_queue  # noqa: E402
from memory_queue import QueueOperationError, processor_error_code  # noqa: E402


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (QueueOperationError("intent_fence_lost"), "intent_fence_lost"),
        (RuntimeError("intent_fence_lost"), "processor_failed:intent_fence_lost"),
        (RuntimeError("provider result: /srv/vault/secret.md"), "processor_failed:RuntimeError"),
        (ValueError(""), "processor_failed:ValueError"),
        (RuntimeError("a" * 200), "processor_failed:RuntimeError"),
    ],
)
def test_the_code_names_the_failure_and_never_its_text(error: BaseException, code: str) -> None:
    assert processor_error_code(error) == code
    assert len(processor_error_code(error).encode("utf-8")) <= memory_queue.PROCESSOR_ERROR_CODE_BYTES


def _raising(error: BaseException):
    def processor(task: dict):
        raise error

    return processor


def _run(processor, task, remaining):
    return processor(task)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (QueueOperationError("intent_fence_lost"), "intent_fence_lost"),
        (RuntimeError("boom!"), "processor_failed:RuntimeError"),
    ],
)
def test_the_worker_hands_the_queue_that_code(
    error: BaseException, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory_queue, "_compat_task", lambda lease: {"id": "task"})
    outcome = memory_queue._run_processor(_raising(error), _run, None, 10.0)

    failure = memory_queue._unsuccessful_worker_failure(outcome)

    assert (outcome.outcome, outcome.error_code, failure.error_code) == (False, code, code)


def test_a_processor_that_answers_false_keeps_the_bare_code() -> None:
    outcome = memory_queue._ProcessorOutcome(False, False, False)

    assert memory_queue._unsuccessful_worker_failure(outcome).error_code == "processor_failed"


def test_the_capture_worker_names_the_same_code() -> None:
    failure = flush_memory._capture_queue_failure(RuntimeError("intent_fence_lost"))

    assert (failure.error_code, failure.retry_after) == ("processor_failed:intent_fence_lost", None)


@pytest.mark.parametrize('character', ['Я', '文', '😀'])
@pytest.mark.parametrize(('permanent', 'expected_state'), [(False, 'ready'), (True, 'dead')])
def test_unicode_reason_settles_the_real_v3_lease(tmp_path, character, permanent, expected_state):
    from tests.test_memory_queue import _v3_queue

    queue = _v3_queue(tmp_path)
    task_id = queue.enqueue('query', 1, {'query': 'plain'})
    lease = queue.claim('unicode-reason-worker')
    assert lease is not None and lease.id == task_id
    error = type(character * 80, (Exception,), {})('private text')
    code = processor_error_code(error)
    assert flush_memory._capture_queue_failure(error).error_code == code
    queue.fail(lease, memory_queue.QueueFailure(code, permanent=permanent))
    task = queue.get(task_id)
    assert task.state == expected_state
    assert task.error_code == code
    assert task.attempt_history[-1].error_code == code
    assert len(code.encode('utf-8')) <= memory_queue.PROCESSOR_ERROR_CODE_BYTES
