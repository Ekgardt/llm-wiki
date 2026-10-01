"""Deferring post-compile work is not a completed maintenance pass."""

from datetime import datetime, timezone

import doctor
import pytest
import scheduled_nightly as nightly


def _state(monkeypatch, status="running"):
    state = {
        nightly.DEFERRED_COMPILE_KEY: "old-start",
        "last_compile_started_at": "old-start",
        "last_compile_status": status,
    }
    monkeypatch.setattr(nightly, "_safe_state", lambda: state)
    monkeypatch.setattr(nightly, "update_state", lambda mutate: mutate(state))
    return state


def test_deferred_pass_does_not_claim_success(monkeypatch):
    state = _state(monkeypatch)
    nightly._record_nightly_result("2026-10-01", 0)
    assert state["last_nightly_status"] == "deferred"
    assert "last_nightly_at" not in state
    assert state[nightly.DEFERRED_COMPILE_KEY] == "old-start"
    assert "deferred" in nightly._nightly_completion_line(0)


def test_live_deferred_compiler_is_not_a_lost_run(monkeypatch):
    state = _state(monkeypatch)
    monkeypatch.setattr(nightly, "_compile_running", lambda: True)
    messages = []
    assert nightly._report_deferred_loss(messages.append) == 0
    assert state[nightly.DEFERRED_COMPILE_KEY] == "old-start"
    assert messages == []


def test_previous_compile_loss_is_seen_before_new_start(monkeypatch):
    state = _state(monkeypatch)
    monkeypatch.setattr(nightly, "_compile_running", lambda: False)
    monkeypatch.setattr(nightly, "_wait_for_compile_idle", lambda log: None)
    monkeypatch.setattr(nightly, "_wait_compile_finished", lambda: True)
    monkeypatch.setattr(nightly, "_report_compile_outcome", lambda *args: 0)
    monkeypatch.setattr(nightly, "_post_compile_pass", lambda *args: 0)

    def new_compile():
        state.update(last_compile_started_at="new-start", last_compile_status="ok")
        return 0

    phases = iter((lambda: 0, new_compile))
    monkeypatch.setattr(nightly, "_run_steps", lambda *args: next(phases)())
    messages = []
    log = nightly.StepLog(messages.append)
    assert nightly._nightly_steps(None, log) == 1
    assert any("old-start" in message and "never finished" in message for message in messages)


def test_doctor_names_deferred_work():
    result = doctor._nightly_result(
        {"last_nightly_status": "deferred"}, datetime.now(timezone.utc), {}
    )
    assert result["status"] == "degraded"
    assert "deferred" in result["message"].lower()


def test_context_names_deferred_work(monkeypatch):
    import session_start_context

    monkeypatch.setattr(session_start_context, "_nightly_state", lambda: {"last_nightly_status": "deferred"})
    assert "deferred" in session_start_context._nightly_line().lower()


def test_actual_failure_wins_over_deferral(monkeypatch):
    state = _state(monkeypatch)
    nightly._record_nightly_result("2026-10-01", 2, "real failure")
    assert state["last_nightly_status"] == "failed"
    assert state["last_nightly_failure"]["failures"] == 2
    assert state["last_nightly_failure"]["error"] == "real failure"


def test_manager_failure_before_compiler_entry_is_not_success(monkeypatch):
    state = _state(monkeypatch, status='starting')
    monkeypatch.setattr(nightly, '_compile_running', lambda: False)
    assert nightly._compile_died_this_pass(state, 'previous-start')
    messages = []
    assert nightly._report_deferred_loss(messages.append) == 1
    assert 'never finished' in messages[0]


def _completed_pass(monkeypatch, status="ok"):
    state = _state(monkeypatch, status)
    state["last_compile_finished_at"] = "old-finish"
    monkeypatch.setattr(nightly, "_compile_running", lambda: False)
    monkeypatch.setattr(nightly, "_run_steps", lambda *args: pytest.fail("new work before owed post-compile pass"))
    messages = []
    return state, nightly.StepLog(messages.append), messages


def test_finished_deferred_compile_resumes_tail_before_new_inputs(monkeypatch):
    state, log, messages = _completed_pass(monkeypatch)
    calls = []
    monkeypatch.setattr(nightly, "_post_compile_pass", lambda *args: calls.append("tail") or 0)

    assert nightly._nightly_steps(None, log) == 0
    assert calls == ["tail"]
    assert nightly.DEFERRED_COMPILE_KEY not in state


def test_failed_deferred_compile_is_reported_while_finishing_its_tail(monkeypatch):
    state, log, messages = _completed_pass(monkeypatch, "error")
    state["last_compile_error"] = "saved compile failure"
    monkeypatch.setattr(nightly, "_post_compile_pass", lambda *args: 0)

    assert nightly._nightly_steps(None, log) == 1
    assert any("saved compile failure" in message for message in messages)
    assert state["last_compile_error"] == "saved compile failure"
    assert nightly.DEFERRED_COMPILE_KEY not in state


def test_failed_tail_keeps_its_deferred_marker_for_retry(monkeypatch):
    state, log, messages = _completed_pass(monkeypatch)
    outcomes = iter((2, 0))
    monkeypatch.setattr(nightly, "_post_compile_pass", lambda *args: next(outcomes))

    assert nightly._nightly_steps(None, log) == 2
    assert state[nightly.DEFERRED_COMPILE_KEY] == "old-start"
    assert nightly._nightly_steps(None, log) == 0
    assert nightly.DEFERRED_COMPILE_KEY not in state


def test_finishing_a_tail_does_not_clear_a_newer_marker(monkeypatch):
    state, log, messages = _completed_pass(monkeypatch)

    def changed_marker(*args):
        state[nightly.DEFERRED_COMPILE_KEY] = "new-start"
        return 0

    monkeypatch.setattr(nightly, "_post_compile_pass", changed_marker)
    with pytest.raises(RuntimeError, match="deferred compile changed"):
        nightly._nightly_steps(None, log)
    assert state[nightly.DEFERRED_COMPILE_KEY] == "new-start"


def test_superseded_compile_does_not_hide_the_missing_outcome(monkeypatch):
    state, log, messages = _completed_pass(monkeypatch)
    state["last_compile_started_at"] = "later-start"
    monkeypatch.setattr(nightly, "_post_compile_pass", lambda *args: 0)

    assert nightly._nightly_steps(None, log) == 1
    assert any("superseded" in message for message in messages)
    assert nightly.DEFERRED_COMPILE_KEY not in state


def test_tail_exception_preserves_the_owed_work(monkeypatch):
    state, log, messages = _completed_pass(monkeypatch)

    def failed_tail(*args):
        raise OSError("post-compile failure")

    monkeypatch.setattr(nightly, "_post_compile_pass", failed_tail)
    with pytest.raises(OSError, match="post-compile failure"):
        nightly._nightly_steps(None, log)
    assert state[nightly.DEFERRED_COMPILE_KEY] == "old-start"


def test_live_compiler_prevents_post_compile_resume(monkeypatch):
    state, log, messages = _completed_pass(monkeypatch)
    monkeypatch.setattr(nightly, "_compile_running", lambda: True)

    assert nightly._resume_deferred_post_compile(None, log) is None
    assert state[nightly.DEFERRED_COMPILE_KEY] == "old-start"
