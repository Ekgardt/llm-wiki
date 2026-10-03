"""A bounded wait cannot establish the lifecycle of an existing compiler."""

from __future__ import annotations


def test_a_deferred_compile_keeps_its_outcome_unknown(monkeypatch):
    import scheduled_nightly

    remembered = []
    messages = []
    monkeypatch.setattr(scheduled_nightly, "_run_steps", lambda *args: 1)
    monkeypatch.setattr(scheduled_nightly, "_wait_for_compile_idle", lambda log: None)
    monkeypatch.setattr(scheduled_nightly, "_last_compile_finished", lambda: None)
    monkeypatch.setattr(scheduled_nightly, "_last_compile_started", lambda: "external-start")
    monkeypatch.setattr(scheduled_nightly, "_wait_compile_finished", lambda: False)
    monkeypatch.setattr(
        scheduled_nightly, "_remember_deferred_compile", lambda log: remembered.append(True)
    )

    failures = scheduled_nightly._nightly_steps(
        lambda *args: None, scheduled_nightly.StepLog(messages.append)
    )

    assert failures == 2
    assert remembered == [True]
    report = "\n".join(messages)
    assert "outcome is unknown" in report
    assert "stops that compile" not in report
