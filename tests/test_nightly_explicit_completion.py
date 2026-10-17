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
    assert str(nightly.ROOT / 'scripts' / 'compile_memory.py') in step.command
    assert str(nightly.ROOT / 'scripts' / 'maybe_compile.py') not in step.command
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


def test_full_health_and_scheduler_share_the_configured_budget(tmp_path, monkeypatch):
    import doctor
    import install_control
    previous = nightly.worst_case_seconds(tmp_path)
    (tmp_path / "llm-wiki.toml").write_text("[doctor]\nnightly_seconds = 3600\n")
    monkeypatch.setattr(nightly, "ROOT", tmp_path)
    monkeypatch.setattr(nightly, "REPORTS_DIR", tmp_path / "logs")
    seen = []
    def diagnose(**kwargs):
        seen.append(kwargs)
        return {"checks": [], "overall_status": "ok"}
    monkeypatch.setattr(doctor, "run_doctor", diagnose)
    assert nightly._write_health_report(lambda message: None) == "complete"
    assert seen[0]["time_budget_seconds"] == 3600
    assert seen[0]["scope"] == "full"
    assert nightly.worst_case_seconds(tmp_path) - previous == 3600 - 60
    assert install_control.scheduler_limit_hours(tmp_path)["nightly"] * 3600 > nightly.worst_case_seconds(tmp_path)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "1.5"])
def test_invalid_health_budget_is_reported_without_running_doctor(tmp_path, monkeypatch, value):
    import doctor
    monkeypatch.setenv("LLM_WIKI_DOCTOR_NIGHTLY_SECONDS", value)
    monkeypatch.setattr(nightly, "ROOT", tmp_path)
    seen = []
    monkeypatch.setattr(doctor, "run_doctor", lambda **kwargs: seen.append(kwargs))
    lines = []
    assert nightly._write_health_report(lines.append) == "failed"
    assert seen == []
    assert "LLM_WIKI_DOCTOR_NIGHTLY_SECONDS" in lines[0]


def test_health_budget_environment_overrides_file(tmp_path, monkeypatch):
    (tmp_path / "llm-wiki.toml").write_text("[doctor]\nnightly_seconds = 3600\n")
    monkeypatch.setenv("LLM_WIKI_DOCTOR_NIGHTLY_SECONDS", "1800")
    assert nightly.setting_value("doctor.nightly_seconds", tmp_path) == 1800
