"""Calendar arithmetic, separate from the answer-generation integration."""
from datetime import date

import calendar_pass

ASKED = date(2022, 4, 4)

def _claim(text: str, inputs: list[str]) -> dict:
    return {"text": text, "citation_ids": ["E1"], "derivation": "difference", "inputs": inputs}


def _answer(*claims: dict) -> dict:
    return {"status": "answered", "claims": list(claims)}


def test_two_dates_make_a_gap_and_one_date_uses_the_day_asked() -> None:
    both = _answer(_claim("The event was on 2022-03-09.", ["2022-03-09", "2022-04-04"]))
    one = _answer(_claim("The event was on 2022-03-09.", ["2022-03-09"]))

    assert [gap[3] for gap in calendar_pass.computed_gaps(both, None)] == [26]
    assert [gap[3] for gap in calendar_pass.computed_gaps(one, ASKED)] == [26]
    assert calendar_pass.computed_gaps(one, None) == []


def test_a_claim_that_states_the_gap_in_days_or_weeks_needs_no_second_pass() -> None:
    in_days = _answer(_claim("You attended it 27 days ago, on 2022-03-09.", ["2022-03-09"]))
    in_weeks = _answer(_claim("About 4 weeks ago, on 2022-03-09.", ["2022-03-09"]))
    silent = _answer(_claim("You attended it on 2022-03-09.", ["2022-03-09"]))

    assert calendar_pass.unstated_gaps(in_days, ASKED) == []
    assert calendar_pass.unstated_gaps(in_weeks, ASKED) == []
    assert len(calendar_pass.unstated_gaps(silent, ASKED)) == 1


def test_the_note_names_both_dates_the_days_and_the_weeks() -> None:
    gaps = calendar_pass.computed_gaps(_answer(_claim("x", ["2022-03-09"])), ASKED)

    note = calendar_pass.calendar_note(gaps)

    assert note.startswith("<calendar>")
    assert "- from 2022-03-09 to 2022-04-04: 26 days (3 weeks and 5 days)" in note
    assert calendar_pass.calendar_note([]) == ""

