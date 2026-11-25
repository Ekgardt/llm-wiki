"""A count, a sum or a difference between quantities is computed by code from the claim's inputs."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
BENCHMARK = Path(__file__).resolve().parents[1] / "benchmark"
for folder in (SCRIPTS, BENCHMARK):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from longmemeval_vault import hypothesis_of  # noqa: E402
from tally_pass import with_computed  # noqa: E402


def _marked(derivation: str, inputs: list[str], text: str) -> dict[str, Any]:
    claim = {"text": text, "derivation": derivation, "inputs": inputs}
    with_computed({"claims": [claim]})
    return claim


def test_a_count_is_the_number_of_distinct_entries_it_listed() -> None:
    claim = _marked(
        "count", ["road bike", "Mountain bike", "road  bike", "e-bike"], "You own two bikes."
    )

    assert claim["computed"] == "3"
    assert claim["computed_stated"] is False


def test_a_count_stated_in_words_is_stated() -> None:
    claim = _marked("count", ["guitar", "piano", "violin"], "You play three instruments.")

    assert claim["computed_stated"] is True


def test_a_sum_adds_one_figure_per_entry() -> None:
    claim = _marked(
        "sum", ["$1,200 for the bike", "$85.50 helmet", "$14.5 lights"], "You spent $1,300."
    )

    assert claim["computed"] == "1300"
    assert claim["computed_stated"] is True


def test_a_sum_over_an_entry_without_one_figure_is_not_guessed() -> None:
    claim = _marked("sum", ["$20 on 2 tickets", "$15"], "You spent $35.")

    assert "computed" not in claim


def test_gpt4_93159ced_the_gap_between_two_durations() -> None:
    claim = _marked(
        "difference",
        ["working professionally for 9 years", "at NovaTech for about 4 years and 3 months"],
        "You worked about 5 years before NovaTech.",
    )

    assert claim["computed"] == "4 years and 9 months (57 months)"
    assert claim["computed_stated"] is False


def test_a_gap_stated_in_months_alone_is_stated() -> None:
    claim = _marked("difference", ["9 years", "4 years and 3 months"], "That is 57 months.")

    assert claim["computed_stated"] is True


def test_a_difference_between_dates_is_left_to_the_calendar() -> None:
    claim = _marked("difference", ["2023-05-01", "2023-05-25"], "24 days.")

    assert "computed" not in claim


def test_durations_in_units_that_do_not_convert_give_no_figure() -> None:
    claim = _marked("difference", ["3 years", "2 weeks"], "About three years.")

    assert "computed" not in claim


def test_a_lookup_claim_is_left_alone() -> None:
    claim = {"text": "Your bike is red.", "inputs": []}
    with_computed({"claims": [claim]})

    assert claim == {"text": "Your bike is red.", "inputs": []}


def test_the_stand_reads_a_figure_the_text_does_not_state() -> None:
    claim = _marked("count", ["a", "b", "c"], "You own two.")

    assert hypothesis_of({"claims": [claim]}) == "You own two. (computed from its inputs: 3)"
    assert (
        hypothesis_of({"claims": [_marked("count", ["a", "b"], "You own two.")]}) == "You own two."
    )


def test_a_text_showing_its_working_does_not_state_the_gap() -> None:
    claim = _marked(
        "difference",
        ["working professionally for 9 years", "at NovaTech for 4 years and 3 months"],
        "Of 9 years, 4 years and 3 months were at NovaTech, so about 5 years before it.",
    )

    assert claim["computed"] == "4 years and 9 months (57 months)"
    assert claim["computed_stated"] is False


def test_a_zero_gap_is_stated_only_when_the_text_says_it() -> None:
    claim = _marked("difference", ["for 2 years", "for 24 months"], "You worked there longer.")

    assert claim["computed"] == "0 years and 0 months (0 months)"
    assert claim["computed_stated"] is False


def test_hours_and_minutes_are_not_subtracted_as_plain_numbers() -> None:
    claim = _marked("difference", ["2 hours", "45 minutes"], "The gap was 43.")

    assert "computed" not in claim


def test_dates_named_by_month_are_left_to_the_calendar() -> None:
    claim = _marked("difference", ["March 9", "April 4"], "It was 5 days.")

    assert "computed" not in claim


def test_amounts_written_in_words_are_read() -> None:
    claim = _marked(
        "difference", ["for one year and 3 months", "for two years and 3 months"], "One year."
    )

    assert claim["computed"] == "1 years and 0 months (12 months)"
    assert claim["computed_stated"] is True


def test_a_rate_is_not_read_as_a_duration() -> None:
    claim = _marked("sum", ["Netflix $15 a month", "Spotify $10 a month"], "You pay $25.")

    assert "computed" not in claim


def test_a_fraction_of_a_unit_gives_no_figure() -> None:
    for inputs in (
        ["a year and a half at Acme", "6 months at Beta"],
        ["half a year in Berlin", "2 years in Paris"],
        ["yoga 3 times a week", "running 2 times a week"],
    ):
        assert "computed" not in _marked("difference", inputs, "Some time.")


def test_a_whole_unit_followed_by_a_smaller_one_does_not_state_the_figure() -> None:
    days = _marked(
        "difference", ["10 days in Rome", "3 days in Paris"], "You stayed 1 week and 2 days longer."
    )
    years = _marked(
        "difference", ["worked 9 years", "5 years at NovaTech"], "About 4 years and 3 months."
    )

    assert (days["computed"], days["computed_stated"]) == ("7 days", False)
    assert years["computed_stated"] is False
    assert _marked("difference", ["10 days", "3 days"], "One week longer.")["computed_stated"]


def test_a_sum_converts_weeks_to_days() -> None:
    claim = _marked("sum", ["3 days in Paris", "1 week in Rome"], "You travelled for 10 days.")

    assert claim["computed"] == "10 days"
    assert claim["computed_stated"] is True


def test_a_sum_of_different_units_is_not_guessed() -> None:
    claim = _marked("sum", ["2 hours of piano", "30 minutes of guitar"], "You practised 32.")

    assert "computed" not in claim


def test_a_figure_too_large_to_compute_exactly_gives_no_figure() -> None:
    huge = "9" * 40
    claim = _marked("difference", [f"{huge} months", "1 month"], "A long time.")

    assert "computed" not in claim
