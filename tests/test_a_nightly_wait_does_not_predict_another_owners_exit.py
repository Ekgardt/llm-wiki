"""A bounded wait cannot establish the lifecycle of an existing compiler."""

from __future__ import annotations


def test_a_deferred_compile_keeps_its_outcome_unknown(monkeypatch):
    import scheduled_nightly

    messages = []
    monkeypatch.setattr(scheduled_nightly, "_run_steps", lambda *args: 1)
    monkeypatch.setattr(scheduled_nightly, "_wait_for_compile_idle", lambda log: None)
    monkeypatch.setattr(scheduled_nightly, "_compile_running", lambda: True)
    log = scheduled_nightly.StepLog(messages.append)
    failures = scheduled_nightly._nightly_steps(lambda *args: None, log)

    assert failures == 1
    assert log.deferred == ["compile_busy"]
    report = "\n".join(messages)
    assert "outcome is unknown" in report
    assert "stops that compile" not in report
