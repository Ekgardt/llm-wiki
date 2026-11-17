"""Original best-span coverage regressions, independent from model execution."""
from collections import namedtuple
from datetime import date

import evidence_sufficiency

Span = namedtuple("Span", "text relative_path")
ASKED = date(2023, 5, 30)


def test_the_unit_is_one_span_not_the_union_of_all_of_them() -> None:
    """Twelve topical distractors say every word between them; one span says them together."""
    scattered = [Span("I finally beat the boss.", "a.md"), Span("A great game night.", "b.md")]
    together = [Span("I finally beat the game's last boss.", "c.md")]
    question = "What game did I finally beat?"

    best_of_scattered = evidence_sufficiency.sufficiency(question, scattered, ASKED)

    assert (best_of_scattered.covered, best_of_scattered.missing) == (("beat", "finally"), ("game",))
    assert evidence_sufficiency.sufficiency(question, together, ASKED).score == 1.0


def test_a_day_inside_the_window_is_an_aspect_and_the_file_name_dates_the_span() -> None:
    """"Last weekend" asked on Tuesday 2023-05-30 is 05-27/28; a Friday 05-26 entry is inside three days."""
    question = "What game did I finally beat last weekend?"
    friday = [Span("I finally beat the game.", "knowledge/daily/2023-05-26.md")]
    far = [Span("I finally beat the game.", "knowledge/daily/2023-05-01.md")]

    near, distant = (
        evidence_sufficiency.sufficiency(question, spans, ASKED) for spans in (friday, far)
    )

    assert evidence_sufficiency.question_window(question, ASKED) == (date(2023, 5, 27), date(2023, 5, 28))
    assert (near.score, near.days_from_window) == (1.0, 1)
    assert (distant.score, distant.days_from_window, distant.missing) == (0.75, 26, ("days 2023-05-27..2023-05-28",))


def test_the_threshold_is_the_frozen_tune_value() -> None:
    covered = evidence_sufficiency.Sufficiency(("a", "b", "c"), ("d",))
    thin = evidence_sufficiency.Sufficiency(("a",), ("b", "c"))
    nothing = evidence_sufficiency.Sufficiency((), ())

    assert evidence_sufficiency.SUFFICIENT_TO_READ_AGAIN == 0.69
    assert (evidence_sufficiency.reads_again(covered), evidence_sufficiency.reads_again(thin)) == (True, False)
    assert (nothing.score, evidence_sufficiency.reads_again(nothing)) == (None, False)
