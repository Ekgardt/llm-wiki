"""Original pure metric tests, moved with unchanged function ASTs."""
from benchmark import selective_forgetting_metrics as runner


def _target(*, usable=True, surfaced=True, in_corpus=True):
    return {"probe_usable": usable, "surfaced": surfaced, "in_corpus": in_corpus}

def test_cohort_summary_counts_probes_separately_from_hits():
    summary = runner.cohort_summary([_target(), _target(surfaced=False), _target(usable=False)])
    assert summary["n"] == 3
    assert summary["probes_usable"] == 2
    assert summary["surfaced"] == 1
    assert summary["surfaced_rate"] == 0.5

def test_cohort_summary_of_an_empty_cohort_has_no_rate():
    assert runner.cohort_summary([])["surfaced_rate"] is None

def _arms(control_surfaced: int, treatment_surfaced: int) -> dict:
    control = [_target(surfaced=index < control_surfaced) for index in range(4)]
    treatment = [_target(surfaced=index < treatment_surfaced) for index in range(4)]
    return {
        "control": {"forget_targets": control, "retain_targets": []},
        "treatment": {"forget_targets": treatment, "retain_targets": [_target()]},
    }

def test_supersession_forget_rate_is_measured_against_the_control_arm():
    metrics = runner._supersession_metrics(_arms(4, 0))
    assert metrics["forget_rate"] == 1.0
    assert metrics["retain_rate"] == 1.0

def test_supersession_forget_rate_falls_when_a_retired_page_still_answers():
    assert runner._supersession_metrics(_arms(4, 1))["forget_rate"] == 0.75

def test_supersession_rate_is_none_when_the_control_proved_nothing():
    assert runner._supersession_metrics(_arms(0, 0))["forget_rate"] is None

def test_ageing_rates_compare_the_same_cohort_before_and_after():
    report = {
        "before": {"forget_targets": [_target(), _target()], "retain_targets": [_target()]},
        "after": {
            "forget_targets": [_target(surfaced=False), _target(surfaced=False)],
            "retain_targets": [_target()],
        },
    }
    metrics = runner._ageing_metrics(report)
    assert metrics["forget_rate"] == 1.0
    assert metrics["retain_rate"] == 1.0

def test_restore_fidelity_is_the_byte_identical_share():
    report = {"results": [{"identical": True}, {"identical": False}], "byte_identical": 1}
    assert runner._restore_metrics(report)["live_fidelity"] == 0.5

def test_legacy_metrics_count_every_archived_page_still_collected():
    report = {"archived_still_collected": ["knowledge/notes/archive/2026/x.md"]}
    assert runner._legacy_metrics(report)["leaks"] == 1

def test_a_leaked_archive_page_fails_its_gate():
    gates = runner._gates_for("legacy", {"leaks": 1})
    assert gates == [("legacy.no_archive_leak", False, "1 leaked")]

def test_a_perfect_rate_passes_and_anything_less_fails():
    assert runner._rate_gate("x", 1.0)[1] is True
    assert runner._rate_gate("x", 0.99)[1] is False
    assert runner._rate_gate("x", None)[1] is False

def test_session_gates_need_both_halves_of_the_window():
    entry = {"aged_record_moved": True, "recent_record_kept": False,
             "archived_bytes_identical": True}
    assert runner._session_gates(entry)[0][1] is False

def test_session_gates_pass_when_the_window_moved_only_the_aged_record():
    entry = {"aged_record_moved": True, "recent_record_kept": True,
             "archived_bytes_identical": True}
    assert [gate[1] for gate in runner._session_gates(entry)] == [True, True]

def test_session_metrics_report_whether_a_restore_command_exists():
    metrics = runner._sessions_metrics({"restore_command_exists": False})
    assert metrics["restore_command_exists"] is False

def _round(forget_surfaced: int, reprieve_surfaced: int) -> dict:
    return {
        "forget_targets": [_target(surfaced=index < forget_surfaced) for index in range(2)],
        "retain_targets": [_target()],
        "reprieve_targets": [_target(surfaced=index < reprieve_surfaced) for index in range(2)],
    }

def test_ageing_reports_the_reprieve_cohort_separately():
    metrics = runner._ageing_metrics({"before": _round(2, 2), "after": _round(0, 2)})
    assert metrics["forget_rate"] == 1.0
    assert metrics["reprieve_rate"] == 1.0

def test_a_reprieved_page_that_was_archived_fails_its_gate():
    metrics = runner._ageing_metrics({"before": _round(2, 2), "after": _round(0, 1)})
    assert metrics["reprieve_rate"] == 0.5
    assert ("ageing.reprieve_rate", False, "0.5") in runner._gates_for("ageing", metrics)

def test_supersession_has_no_reprieve_gate():
    names = [gate[0] for gate in runner._gates_for("supersession", _arms(4, 0))]
    assert names == ["supersession.forget_rate", "supersession.retain_rate"]
