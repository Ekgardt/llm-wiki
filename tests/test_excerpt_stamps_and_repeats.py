"""An excerpt is dated by its entry, and a passage its entry already said is read once."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from evidence_repeats import repeated  # noqa: E402
from excerpt_stamps import EntryStamps, ExcerptStamp  # noqa: E402

DAY = (
    b"# 2024-03-02\n\n"
    b"## [09:15:00] session_end | sess-a\n\n**user:** first\n\n"
    b"## [18:40:07] manual decision\n\nUse SQLite.\n"
)


def test_a_daily_excerpt_carries_its_entry_time_and_session() -> None:
    stamps = EntryStamps.of("knowledge/daily/2024-03-02.md", DAY)

    assert stamps.at(DAY.index(b"first")) == ExcerptStamp("2024-03-02 09:15:00", "sess-a")
    assert stamps.at(DAY.index(b"Use SQLite")) == ExcerptStamp("2024-03-02 18:40:07", "")


def test_text_before_the_first_entry_carries_the_day() -> None:
    assert EntryStamps.of("knowledge/daily/2024-03-02.md", DAY).at(0) == ExcerptStamp("2024-03-02")


def test_a_page_is_dated_by_its_valid_from_and_has_no_session() -> None:
    stamps = EntryStamps.of("knowledge/notes/storage.md", b"## [09:15:00] x | y\n", "2024-01-05")

    assert stamps.at(5) == ExcerptStamp("2024-01-05")


SAID = "I have been working at NovaTech for about four years and three months now"


def test_a_restatement_in_the_same_entry_is_a_repeat() -> None:
    passages = [("a", SAID + ", which tool fits?"), ("a", "To start with, " + SAID)]

    assert repeated(passages) == {1}


def test_the_same_words_in_another_entry_are_kept() -> None:
    assert repeated([("a", SAID), ("b", SAID)]) == set()


def test_a_passage_with_a_new_fact_is_kept() -> None:
    passages = [("a", "EHR data can be noisy, incomplete, or inconsistent."),
                ("a", "Wearable device data can be noisy, incomplete, or inconsistent.")]

    assert repeated(passages) == set()


def test_a_passage_too_short_to_compare_is_kept() -> None:
    assert repeated([("a", "Legal"), ("a", "Legal")]) == set()


def test_a_restatement_that_corrects_a_figure_is_kept() -> None:
    first = "I set aside a budget of 500 dollars for the new road bike and its gear this spring"
    passages = [("a", first), ("a", first.replace("500", "900"))]

    assert repeated(passages) == set()


BREADCRUMB_DAY = (
    b"# 2024-03-02\n\n"
    b"## [09:15:00] session_end | sess-a\n\n**user:** first\n\n"
    b"<!-- llm-wiki-operation:" + b"ab" * 32 + b" -->\n"
    b"- `[15:42:10] prompt | sessB123 | proj` second\n"
)


def test_an_operation_marker_entry_after_a_heading_carries_its_own_stamp() -> None:
    stamps = EntryStamps.of("knowledge/daily/2024-03-02.md", BREADCRUMB_DAY)

    assert stamps.at(BREADCRUMB_DAY.index(b"first")) == ExcerptStamp(
        "2024-03-02 09:15:00", "sess-a"
    )
    assert stamps.at(BREADCRUMB_DAY.index(b"second")) == ExcerptStamp(
        "2024-03-02 15:42:10", "sessB123"
    )


def test_a_heading_padded_after_its_pipe_is_still_stamped() -> None:
    day = b"## [09:15:00] x |" + b" " * 100_000 + b"\nbody\n"

    stamps = EntryStamps.of("knowledge/daily/2024-03-02.md", day)

    assert stamps.at(day.index(b"body")) == ExcerptStamp("2024-03-02 09:15:00", "")


def test_a_long_correction_of_a_figure_in_one_session_is_kept() -> None:
    said = (
        "For the trip to Lisbon next month I am planning a total budget of 500 dollars covering"
        " the hotel, the flights, food, museum tickets and a day trip to Sintra with my sister"
        " and her husband."
    )
    passages = [("s", said), ("s", "Sorry, correction: " + said.replace("500", "900"))]

    assert repeated(passages) == set()


def test_a_tool_line_is_stamped_with_its_session_not_its_agent() -> None:
    marker = b"<!-- llm-wiki-operation:" + b"cd" * 32 + b" -->\n"
    day = (
        b"# 2026-11-20\n\n" + marker
        + b"- `[15:42:10] tool | claude | sessB123 | proj | Edit` scripts/a.py\n"
        + marker.replace(b"cd", b"ef")
        + b"- `[16:00:00] episodes | 2026-11-20` consolidated\n"
    )
    stamps = EntryStamps.of("knowledge/daily/2026-11-20.md", day)

    assert stamps.at(day.index(b"scripts/a.py")).session == "sessB123"
    assert stamps.at(day.index(b"consolidated")).session == ""
