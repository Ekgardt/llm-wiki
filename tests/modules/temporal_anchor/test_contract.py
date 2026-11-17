"""Original pure query-date checks, independent from answer orchestration."""
from datetime import date

from temporal_anchor import query_with_dates, window

ASKED = date(2022, 4, 4)


def test_weeks_ago_in_a_question_reach_the_days_around_the_resolved_day() -> None:
    expanded = query_with_dates("What did I buy four weeks ago?", ASKED)

    assert "2022-03-07" in expanded
    assert "2022-03-04" in expanded and "2022-03-10" in expanded
    assert "2022-03-03" not in expanded
    assert "2022-03-31" not in query_with_dates("What did I buy three days ago?", ASKED)


def test_the_window_covers_the_resolved_days_three_out_each_side() -> None:
    asked = date(2023, 5, 30)

    assert window("Which book did I finish a week ago?", asked) == ("2023-05-20", "2023-05-26")
    assert window("How many bikes do I own?", asked) is None
