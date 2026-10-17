"""A scheduled pass observes its own compiler and distinguishes deferral from completion."""
import pytest
import scheduled_nightly as nightly


def test_compiler_child_budget_includes_its_generation_refresh(tmp_path, monkeypatch):
    (tmp_path / "llm-wiki.toml").write_text("[generation]\npost_compile_seconds = 6919\n")
    monkeypatch.setattr(nightly, "ROOT", tmp_path)
    monkeypatch.setenv(nightly.COMPILE_WAIT_ENV, "1800")
    assert nightly._compile_step().timeout >= 1800 + 6919 + nightly.STEP_START_MARGIN_SECONDS


def test_scheduler_budget_uses_the_requested_vaults_compile_refresh(tmp_path):
    previous_refresh = nightly.setting_value("generation.post_compile_seconds", tmp_path)
    before = nightly.worst_case_seconds(tmp_path)
    (tmp_path / "llm-wiki.toml").write_text("[generation]\npost_compile_seconds = 6919\n")
    after = nightly.worst_case_seconds(tmp_path)
    assert after - before == 6919 - previous_refresh


def test_nightly_waits_for_its_compile_process_not_detached_trigger():
    step = nightly._compile_step()
    assert any(argument.endswith('/compile_memory.py') for argument in step.command)
    assert not any(argument.endswith('/maybe_compile.py') for argument in step.command)
    assert step.timeout > nightly.compile_wait_seconds()


def test_deferred_night_does_not_advance_last_success(monkeypatch):
    state = {'last_nightly_date': '2026-10-16', 'nightly_catchup_claim': {'date': '2026-10-17'}}
    monkeypatch.setattr(nightly, 'update_state', lambda mutate: mutate(state))
    nightly._record_nightly_result('2026-10-17', failures=0, deferred=('compile_busy',))
    assert state['last_nightly_status'] == 'deferred'
    assert state['last_nightly_date'] == '2026-10-16'
    assert state['last_nightly_result']['deferred'] == ['compile_busy']
    assert 'nightly_catchup_claim' not in state


@pytest.mark.parametrize('failures,deferred', [(0, ('compile_busy',)), (0, ('full_health_incomplete',)), (1, ())])
def test_recorded_attempt_does_not_repeat_on_each_session(monkeypatch, failures, deferred):
    import session_start_context as context

    state = {'last_nightly_date': '2026-10-16'}
    monkeypatch.setattr(nightly, 'update_state', lambda mutate: mutate(state))
    monkeypatch.setattr(context, 'update_state', lambda mutate, **kwargs: mutate(state))
    nightly._record_nightly_result('2026-10-17', failures, deferred=deferred)
    assert context._claim_nightly_catchup('2026-10-17') is False
    assert state['last_nightly_date'] == '2026-10-16'
    assert context._claim_nightly_catchup('2026-10-18') is True


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
