"""The selective-forgetting stand must be trustworthy before its numbers are.

These tests cover the stand's own logic — probe extraction, cohort selection,
rate arithmetic and gate verdicts — not the product's archiving, which the
stand measures in throwaway vaults. Nothing here writes into the live vault.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "benchmark"))

import run_selective_forgetting as runner  # noqa: E402
import selective_forgetting_vault as stand  # noqa: E402

PAGE = (
    "---\ntype: debugging\nstatus: active\nsuperseded_by: \"[[a-newer-page]]\"\n---\n\n"
    "# Title\n\n"
    "One-sentence summary: a short line.\n\n"
    "The longest plain body line in this page states the finding in full words.\n\n"
    "- a bullet that should never be chosen as a probe even when it is long enough\n"
)


def test_probe_phrase_takes_a_verbatim_plain_line():
    phrase = stand.probe_phrase(PAGE)
    assert phrase in PAGE
    assert phrase.startswith("The longest plain body line")


def test_probe_phrase_is_clipped_to_a_fixed_word_count():
    assert len(stand.probe_phrase(PAGE).split()) == stand.PROBE_WORDS


def test_probe_phrase_is_deterministic_across_calls():
    assert stand.probe_phrase(PAGE) == stand.probe_phrase(PAGE)


def test_probe_phrase_skips_headings_bullets_and_links():
    phrase = stand.probe_phrase(PAGE)
    assert not phrase.startswith(("#", "-", "One-sentence"))


def test_probe_phrase_is_empty_when_no_line_qualifies():
    assert stand.probe_phrase("---\ntype: gap\n---\n\n# Short\n\ntiny\n") == ""


def test_frontmatter_fields_are_read_without_quotes():
    frontmatter = stand.frontmatter_of(PAGE)
    assert stand.field(frontmatter, "type") == "debugging"
    assert stand.field(frontmatter, "status") == "active"


def test_successor_slugs_read_the_wikilink_target():
    assert stand.successor_slugs(PAGE) == ["a-newer-page"]


def test_successor_slugs_strip_a_path_qualified_link():
    page = "---\nsuperseded_by: [[knowledge/notes/newer-decision]]\n---\n\nbody\n"
    assert stand.successor_slugs(page) == ["newer-decision"]


def test_successor_slugs_are_empty_without_the_field():
    assert stand.successor_slugs("---\ntype: decision\n---\n\nbody\n") == []


def test_source_pages_skips_editorial_metadata(tmp_path):
    for name in ("index.md", "log.md", "README.md", "real-page.md"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    assert [page.name for page in stand.source_pages(tmp_path)] == ["real-page.md"]


def test_seed_copies_only_the_pages_and_returns_the_copies(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "one.md").write_text("body one", encoding="utf-8")
    (source / "index.md").write_text("editorial", encoding="utf-8")
    root = tmp_path / "vault"
    (root / "knowledge/notes").mkdir(parents=True)
    copies = stand.seed(root, source)
    assert [page.name for page in copies] == ["one.md"]
    assert (root / "knowledge/notes/one.md").read_text(encoding="utf-8") == "body one"


def test_rank_of_finds_the_target_by_file_name():
    paths = ["knowledge/notes/other.md", "knowledge/notes/target.md"]
    assert stand._rank_of("target", paths) == 2
    assert stand._rank_of("absent", paths) is None


def test_probe_result_reports_an_unusable_probe_rather_than_a_miss():
    result = stand.probe_result("slug", "")
    assert result["probe_usable"] is False
    assert result["surfaced"] is False


def test_outcome_counts_group_by_the_leading_word():
    counts = stand._outcome_counts(["ARCHIVED: a", "ARCHIVED: b", "WRITE_ERROR: c"])
    assert counts == {"ARCHIVED": 2, "WRITE_ERROR": 1}


# -- the aggregator -------------------------------------------------------


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


def test_aggregate_fails_the_run_when_a_phase_errored():
    report = runner.aggregate({"legacy": {"error": "boom"}}, 0.0)
    assert report["passed"] is False
    assert report["errors"] == {"legacy": "boom"}


def test_aggregate_passes_a_clean_legacy_phase():
    report = runner.aggregate({"legacy": {"archived_still_collected": []}}, 0.0)
    assert report["passed"] is True


def test_decoded_names_an_unparsable_report_instead_of_raising():
    decoded = runner._decoded("legacy", "treatment", "not json\n")
    assert "unparsable report" in decoded["error"]


def test_decoded_names_an_empty_report():
    assert runner._decoded("legacy", "treatment", "")["error"] == "phase produced no report"


def test_the_control_arm_is_the_only_one_that_forces_pages_active():
    control = runner._command("supersession", Path("/w"), Path("/p"), 1, "control")
    treatment = runner._command("supersession", Path("/w"), Path("/p"), 1, "treatment")
    assert "--keep-active" in control
    assert "--keep-active" not in treatment


@pytest.mark.parametrize("phase", runner.PHASE_ORDER)
def test_every_ordered_phase_is_runnable_by_the_vault_script(phase):
    assert phase in stand.PHASES


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
