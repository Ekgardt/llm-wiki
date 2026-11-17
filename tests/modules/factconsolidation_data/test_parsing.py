"""Original deterministic frame and fixture-version assertions."""
import json
import sys

import pytest

from tests.modules.factconsolidation_data.parsing_helpers import ROOT

sys.path.insert(0, str(ROOT / "benchmark"))

import factconsolidation_data as data  # noqa: E402


def test_a_frame_reads_a_numbered_fact_into_subject_property_and_value():
    fact = data.parse_fact("1. The chairperson of Fatah is Mahmoud Abbas.")
    assert fact == data.ParsedFact(1, "Fatah", "chairperson", "Mahmoud Abbas")


def test_a_line_no_frame_claims_is_reported_rather_than_guessed_at():
    facts, unparsed = data.parse_context(
        "0. The chairperson of Fatah is Mahmoud Abbas.\n"
        "1. The origianl broadcaster of The Brady Bunch is ABC."
    )
    assert len(facts) == 1
    assert unparsed == ("1. The origianl broadcaster of The Brady Bunch is ABC.",)


def test_a_key_whose_value_never_changes_is_not_a_conflict():
    facts, _ = data.parse_context(
        "0. The capital of Romania is Bucharest.\n"
        "5. The capital of Romania is Bucharest."
    )
    assert data.conflicts(facts, []) == ()


def test_a_conflict_orders_its_observations_and_takes_the_highest_serial_as_truth():
    facts, _ = data.parse_context(
        "9. The capital of Romania is Rajanpur.\n"
        "0. The capital of Romania is Bucharest."
    )
    found = data.conflicts(facts, [])
    assert len(found) == 1
    assert found[0].observations == ((0, "Bucharest"), (9, "Rajanpur"))
    assert found[0].truth == "Rajanpur"


def test_ground_truth_is_gold_confirmed_only_when_the_dataset_picks_the_newer_value():
    facts, _ = data.parse_context(
        "0. The capital of Romania is Bucharest.\n"
        "9. The capital of Romania is Rajanpur."
    )
    assert data.conflicts(facts, ["Rajanpur"])[0].gold_confirmed is True
    assert data.conflicts(facts, ["Bucharest"])[0].gold_confirmed is False
    assert data.conflicts(facts, [])[0].gold_confirmed is False


def test_a_fixture_from_another_version_is_refused_rather_than_read(tmp_path):
    path = tmp_path / "other.json"
    path.write_text(json.dumps({"schema_version": "conflict/v0"}), encoding="utf-8")
    with pytest.raises(ValueError, match="conflict-resolution/v1"):
        data.load_fixture(path)
