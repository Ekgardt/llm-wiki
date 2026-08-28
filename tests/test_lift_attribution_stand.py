"""The MEM-15 stand, checked where it could quietly lie.

Three things are load-bearing and each has a test that fails when it breaks:
the grader is blind to the product, the gold cannot drift away from the stand
it was inherited from, and a token match is a real match rather than a
substring coincidence. The rest pins the arithmetic that turns two verdicts
into lift, neutral or harm.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _extra in (ROOT / "benchmark", ROOT / "scripts"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

import lift_corpus  # noqa: E402
import run_lift_attribution as runner  # noqa: E402


def _case(**overrides) -> lift_corpus.Case:
    fields = {
        "case_id": "sample",
        "stratum": lift_corpus.WORLD,
        "question": "q",
        "expected_tokens": ("15",),
        "forbidden_tokens": (),
        "collision_probes": ("cyclomatic",),
        "gold_evidence": "15",
    }
    fields.update(overrides)
    return lift_corpus.Case(**fields)


def _stratum_size(cases: list, name: str) -> int:
    return sum(1 for case in cases if case.stratum == name)


def _world_cases() -> list:
    return lift_corpus.load_corpus()["cases"]


def _world_ids_failing(predicate) -> list:
    return [case["case_id"] for case in _world_cases() if not predicate(case)]


def _gold_states_a_token(case: dict) -> bool:
    gold = case["gold_evidence"].casefold()
    return any(token.casefold() in gold for token in case["expected_tokens"])


def _product_names_in_grader() -> list:
    text = Path(lift_corpus.__file__).read_text(encoding="utf-8")
    forbidden = ("search_memory", "mcp_server", "retrieval_paths", "query_memory")
    return [name for name in forbidden if name in text]


def _all_harm_rows(count: int) -> list:
    stratum = lift_corpus.WORLD
    return [
        {"case_id": str(i), "stratum": stratum, "outcome": lift_corpus.HARM}
        for i in range(count)
    ]


def test_the_corpus_validates_and_carries_both_strata():
    cases = lift_corpus.all_cases(lift_corpus.load_corpus())
    assert _stratum_size(cases, lift_corpus.VAULT) > 0
    assert _stratum_size(cases, lift_corpus.WORLD) > 0


def test_every_world_gold_is_backed_by_a_recorded_command():
    """A gold nobody can re-derive is an opinion. Each world case names the
    command that produced it and the output that command printed."""
    assert _world_ids_failing(lambda case: bool(case["gold_command"].strip())) == []


def test_the_expected_token_actually_appears_in_the_recorded_gold():
    assert _world_ids_failing(_gold_states_a_token) == []


def _source_tokens() -> dict:
    source = json.loads(lift_corpus.INHERITED_CORPUS.read_text(encoding="utf-8"))
    return {case["case_id"]: tuple(case["expected_tokens"]) for case in source["cases"]}


def _inherited_tokens() -> dict:
    return {case.case_id: case.expected_tokens for case in lift_corpus.inherited_cases()}


def test_the_vault_stratum_cannot_drift_from_the_stand_it_came_from():
    """Inherited, not copied: if the application corpus changes, this follows."""
    assert _inherited_tokens() == _source_tokens()


def test_the_grader_never_imports_the_product_retrieval():
    """The rubric must not see the product's retrieval, prompt or envelope.

    Read off the module rather than promised in a docstring: `lift_corpus` is
    the only thing that decides correct or wrong, and it may not reach into
    search, the MCP server or the answer envelope to do it.
    """
    assert _product_names_in_grader() == []


def test_a_shorter_number_does_not_satisfy_a_longer_one():
    assert lift_corpus.grade("the default is 15", _case()) is True
    assert lift_corpus.grade("the default is 5", _case()) is False


def test_a_longer_number_does_not_satisfy_a_shorter_one():
    five = _case(expected_tokens=("5",))
    assert lift_corpus.grade("threshold 15", five) is False
    assert lift_corpus.grade("threshold 5", five) is True


def test_a_forbidden_token_fails_an_otherwise_matching_answer():
    hedged = _case(expected_tokens=("15",), forbidden_tokens=("5",))
    assert lift_corpus.grade("15 is the default", hedged) is True
    assert lift_corpus.grade("15, but here we use 5", hedged) is False


def test_a_punctuated_token_is_matched_literally():
    flag = _case(expected_tokens=("--ff-only",))
    assert lift_corpus.grade("it merges with --ff-only", flag) is True
    assert lift_corpus.grade("it merges fast-forward", flag) is False


def test_an_empty_or_missing_answer_is_wrong_not_skipped():
    assert lift_corpus.grade(None, _case()) is False
    assert lift_corpus.grade("", _case()) is False


@pytest.mark.parametrize(
    ("without", "with_memory", "expected"),
    [
        (False, True, lift_corpus.LIFT),
        (True, False, lift_corpus.HARM),
        (True, True, lift_corpus.NEUTRAL),
        (False, False, lift_corpus.NEUTRAL),
    ],
)
def test_the_three_outcomes_are_exactly_the_four_verdict_pairs(without, with_memory, expected):
    assert lift_corpus.classify(without, with_memory) == expected


def test_harm_is_reported_as_its_own_fraction_and_not_folded_into_net():
    """The point of the stand: harm has to be visible on its own."""
    report = lift_corpus.summarise([lift_corpus.LIFT] * 3 + [lift_corpus.HARM])
    assert report["harm_rate"] == 0.25
    assert report["net_lift_rate"] == 0.5


def test_an_empty_stratum_reports_none_rather_than_zero():
    """Zero would read as `measured, and it was nothing`. None reads as unmeasured."""
    report = lift_corpus.summarise([])
    assert report["n"] == 0
    assert report["harm_rate"] is None


def test_the_bootstrap_interval_is_seeded_and_brackets_the_estimate():
    outcomes = [lift_corpus.LIFT] * 8 + [lift_corpus.NEUTRAL] * 10 + [lift_corpus.HARM] * 5
    first = lift_corpus.bootstrap_net_ci(outcomes)
    assert first == lift_corpus.bootstrap_net_ci(outcomes)
    assert first["low"] <= lift_corpus.summarise(outcomes)["net_lift_rate"] <= first["high"]


def test_a_delta_inside_the_providers_own_disagreement_is_not_a_win():
    """NEW-122: 2 of 23 at a byte-identical prompt is 8.7 points."""
    assert lift_corpus.indistinguishable(0.043, 8.7) is True
    assert lift_corpus.indistinguishable(-0.043, 8.7) is True
    assert lift_corpus.indistinguishable(0.30, 8.7) is False


def test_an_unmeasured_delta_is_treated_as_noise_not_as_signal():
    assert lift_corpus.indistinguishable(None, 8.7) is True


def test_collision_degree_counts_documents_and_is_zero_when_absent(tmp_path):
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "knowledge" / "a.md").write_text("about journal_mode", encoding="utf-8")
    assert lift_corpus.collision_degree(tmp_path, ("journal_mode",)) == 1
    assert lift_corpus.collision_degree(tmp_path, ("nothing-here",)) == 0


def _world_collision_degrees() -> list:
    corpus = lift_corpus.load_corpus()
    probes = [tuple(case["collision_probes"]) for case in corpus["cases"]]
    return [lift_corpus.collision_degree(ROOT, probe) for probe in probes]


def test_the_live_vault_has_real_collision_on_the_world_probes():
    """The analogue is only honest if the collision it claims exists here."""
    assert max(_world_collision_degrees()) > 1


def test_both_conditions_get_the_same_instruction():
    """Only the notes differ. A differing instruction would confound the delta."""
    case = _case(question="how long is a sha256 hexdigest")
    assert case.question in runner.closed_book_prompt(case)
    assert case.question in runner.with_memory_prompt(case, "notes")
    assert "notes" in runner.with_memory_prompt(case, "notes")


def test_the_notes_block_is_bounded_by_row_count_and_by_size():
    pool = [{"path": f"p{i}.md", "text": "x" * 5000} for i in range(20)]
    notes = runner.render_notes(pool, ROOT)
    assert notes.count("--- p") == runner.MAX_NOTES
    assert len(notes) < runner.MAX_NOTES * (runner.MAX_NOTE_CHARS + 200)


def test_a_row_without_text_falls_back_to_the_page_it_names(tmp_path):
    (tmp_path / "page.md").write_text("body of the page", encoding="utf-8")
    notes = runner.render_notes([{"path": "page.md"}], tmp_path)
    assert "body of the page" in notes


def test_a_row_naming_a_missing_page_does_not_crash_the_run(tmp_path):
    notes = runner.render_notes([{"path": "gone.md"}], tmp_path)
    assert "gone.md" in notes


def test_the_noise_probe_counts_flips_on_the_identical_prompt():
    rows_ = [
        {"case_id": "a", "correct_with": True},
        {"case_id": "b", "correct_with": False},
    ]
    probes = [
        {"case_id": "a", "probe_correct": False},
        {"case_id": "b", "probe_correct": False},
    ]
    assert runner._disagreement(rows_, probes) == {"n": 2, "disagreements": 1, "points": 50.0}


def test_no_probe_reports_unmeasured_rather_than_a_clean_floor():
    assert runner._disagreement([{"case_id": "a", "correct_with": True}], [])["points"] is None


def test_the_report_names_harm_and_the_noise_verdict_at_the_top_level():
    corpus = lift_corpus.load_corpus()
    rows_ = [
        {"case_id": "a", "stratum": lift_corpus.WORLD, "outcome": lift_corpus.HARM},
        {"case_id": "b", "stratum": lift_corpus.VAULT, "outcome": lift_corpus.LIFT},
    ]
    report = runner.build_report(corpus, rows_, {"retrieval_path": "mcp"}, [])
    assert report["overall"]["harm"] == 1
    assert "net_lift_indistinguishable_from_noise" in report["thresholds"]


def test_a_harm_rate_over_the_threshold_is_reported_as_over_it():
    corpus = lift_corpus.load_corpus()
    rows_ = _all_harm_rows(4)
    report = runner.build_report(corpus, rows_, {}, [])
    assert report["thresholds"]["harm_within_limit"] is False


def test_a_baseline_report_is_reused_instead_of_being_scored_again(tmp_path):
    """The noise probe must not pay for the whole stand a second time."""
    report = tmp_path / "run1.json"
    report.write_text(json.dumps({"cases": [{"case_id": "a", "outcome": "lift"}]}), encoding="utf-8")
    reused = runner._baseline_rows(str(report), [], None, {}, ROOT)
    assert reused == [{"case_id": "a", "outcome": "lift"}]


def test_outcomes_are_also_banded_by_how_contested_the_tokens_are():
    """The paper's collision axis, measured here rather than swept."""
    rows_ = [
        {"case_id": "a", "outcome": lift_corpus.HARM, "collision_degree": 40},
        {"case_id": "b", "outcome": lift_corpus.LIFT, "collision_degree": 0},
    ]
    banded = lift_corpus.by_collision(rows_)
    assert banded["high"]["harm"] == 1
    assert banded["none"]["lift"] == 1


def _recorded_report(tmp_path: Path, answer_with: str) -> str:
    report = tmp_path / "run.json"
    row = {
        "case_id": "sample",
        "stratum": lift_corpus.WORLD,
        "answer_without": "the default is 5",
        "answer_with": answer_with,
        "correct_without": False,
        "correct_with": True,
        "outcome": lift_corpus.LIFT,
    }
    report.write_text(json.dumps({"cases": [row]}), encoding="utf-8")
    return str(report)


def test_a_finished_run_can_be_rejudged_from_its_recorded_answers(tmp_path):
    """A rubric correction must be a re-judgement, not a re-roll of the model."""
    path = _recorded_report(tmp_path, "the default is 15")
    rows_ = runner.regraded([_case()], path)
    assert rows_[0]["outcome"] == lift_corpus.LIFT
    assert rows_[0]["answer_with"] == "the default is 15"


def test_rejudging_can_move_an_outcome_without_touching_the_answers(tmp_path):
    path = _recorded_report(tmp_path, "the default is 5")
    rows_ = runner.regraded([_case()], path)
    assert rows_[0]["outcome"] == lift_corpus.NEUTRAL
