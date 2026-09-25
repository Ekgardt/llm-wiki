"""An absent provider is a stated wait; a task's last failure is recorded as a loss.

See `docs/research/2026-09-17-an-absent-provider-is-waited-for-and-a-spent-task-is-a-loss.md`.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import capture_diagnostics  # noqa: E402
import flush_memory  # noqa: E402
import llm_client  # noqa: E402
import operational_ownership  # noqa: E402

from tests.test_queue_v3_capture_links import (  # noqa: E402
    _capture_binding,
    _coordinator,
    _queue,
)

RECORD = {"event": "session_end", "evidence": "a short session"}


@pytest.fixture(autouse=True)
def _a_provider_that_cannot_answer(monkeypatch) -> None:
    """The real chain, forced to a provider with no key: it answers None."""
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def _ask_absent_provider(*_args, **_kwargs):
    return flush_memory._call_capture_classifier(RECORD, None)


def test_the_real_chain_with_no_provider_is_a_wait() -> None:
    assert llm_client.call_llm_result("prompt", "system", 100) is None
    with pytest.raises(flush_memory.CaptureProviderUnavailable):
        _ask_absent_provider()


def _error_code(queue, task_id: str) -> tuple[str, object]:
    with sqlite3.connect(queue.db_path) as database:
        row = database.execute(
            "SELECT state, error_code FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    return str(row[0]), row[1]


def test_an_absent_provider_is_named_to_the_queue(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    binding = _capture_binding(
        queue, coordinator, registry, intent_id="a" * 64, intent_sha256="b" * 64
    )

    with pytest.raises(flush_memory.CaptureProviderUnavailable):
        flush_memory.run_capture_worker_once(
            queue, coordinator, process_missing=_ask_absent_provider
        )

    assert _error_code(queue, binding.task_id) == ("ready", "provider_unavailable")


def test_an_absent_provider_states_an_hour_and_other_failures_state_nothing() -> None:
    absent = flush_memory._capture_queue_failure(flush_memory.CaptureProviderUnavailable("x"))
    other = flush_memory._capture_queue_failure(RuntimeError("x"))

    assert (absent.error_code, absent.retry_after) == ("provider_unavailable", 3600)
    assert (other.error_code, other.retry_after) == ("processor_failed:x", None)


def test_the_last_attempt_is_raised_as_a_loss_and_an_earlier_one_is_not() -> None:
    cause = RuntimeError("the real failure")

    flush_memory._raise_if_attempts_spent(SimpleNamespace(attempt=7), cause)
    with pytest.raises(capture_diagnostics.DurableWorkExhausted) as raised:
        flush_memory._raise_if_attempts_spent(SimpleNamespace(attempt=8), cause)

    assert raised.value.__cause__ is cause


def test_a_spent_task_is_recorded_lost_and_a_retried_one_deferred() -> None:
    kind = "adapter_capture_worker"
    spent = capture_diagnostics.DurableWorkExhausted("spent")

    assert capture_diagnostics._outcome_of(spent, kind) == "lost"
    assert capture_diagnostics._outcome_of(RuntimeError("again later"), kind) == "deferred"
