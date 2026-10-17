"""A scheduled pass observes its own compiler and distinguishes deferral from completion."""
import pytest
import scheduled_nightly as nightly


def test_nightly_waits_for_its_compile_process_not_detached_trigger():
    step = nightly._compile_step()
    assert any(argument.endswith('/compile_memory.py') for argument in step.command)
    assert not any(argument.endswith('/maybe_compile.py') for argument in step.command)
    assert step.timeout == nightly.compile_wait_seconds()


def test_deferred_night_does_not_advance_last_success(monkeypatch):
    state = {'last_nightly_date': '2026-10-16', 'nightly_catchup_claim': {'date': '2026-10-17'}}
    monkeypatch.setattr(nightly, 'update_state', lambda mutate: mutate(state))
    nightly._record_nightly_result('2026-10-17', failures=0, deferred=('compile_busy',))
    assert state['last_nightly_status'] == 'deferred'
    assert state['last_nightly_date'] == '2026-10-16'
    assert state['last_nightly_result']['deferred'] == ['compile_busy']
    assert 'nightly_catchup_claim' not in state


@pytest.mark.parametrize("child_result", [0, 1, 2])
@pytest.mark.parametrize("historical_status", ["ok", "error", "running"])
def test_only_this_child_result_controls_dependent_steps(monkeypatch, child_result, historical_status):
    seen = []
    monkeypatch.setattr(nightly, "load_state", lambda: {"last_compile_status": historical_status})
    monkeypatch.setattr(nightly, "_intake_steps", lambda: [])
    monkeypatch.setattr(nightly, "_wait_for_compile_idle", lambda log: None)
    monkeypatch.setattr(nightly, "_compile_running", lambda: False)
    monkeypatch.setattr(nightly, "_post_compile_pass", lambda *args: seen.append("post") or 0)

    def run(command, log, name, *, timeout):
        return child_result if name == "compile" else 0

    failures = nightly._nightly_steps(run, nightly.StepLog(lambda message: None))
    assert failures == int(child_result != 0)
    assert seen == (["post"] if child_result == 0 else [])


@pytest.mark.parametrize("details, outcome", [
    ({"budget_exhausted": True}, "deferred"),
    ({"exception": "RuntimeError"}, "failed"),
    ({"read_error": True}, "failed"),
    ({}, "complete"),
])
def test_full_diagnosis_completion_is_separate_from_its_findings(details, outcome):
    report = {"overall_status": "error", "checks": [{"status": "error", "details": details}]}
    assert nightly._health_report_outcome(report) == outcome


def test_incomplete_full_diagnosis_defers_nightly_completion(monkeypatch):
    monkeypatch.setattr(nightly, "_post_compile_steps", lambda: [])
    monkeypatch.setattr(nightly, "_refresh_generation", lambda log: 0)
    monkeypatch.setattr(nightly, "_compact_telemetry", lambda log: None)
    monkeypatch.setattr(nightly, "_write_health_report", lambda log: "deferred")
    log = nightly.StepLog(lambda message: None)
    assert nightly._post_compile_pass(lambda *args: 0, log) == 0
    assert log.deferred == ["full_health_incomplete"]
