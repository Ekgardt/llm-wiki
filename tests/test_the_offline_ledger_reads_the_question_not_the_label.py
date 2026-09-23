"""The offline ledger proof counts by the question's own words and scores by the label only.

The stand-in extractor posts one record per user turn naming the counted kind over the
whole haystack; the split is a digest of the question id, never its type. Research:
`docs/research/2026-09-22-a-ledger-of-things-and-events-posted-once.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

BENCHMARK = Path(__file__).resolve().parents[1] / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import ledger_offline as offline  # noqa: E402

QUESTION = {
    "question": "How many bikes did I service in March?",
    "question_date": "2023/03/20 (Mon) 23:57",
    "haystack_dates": ["2023/03/20 (Mon) 07:32", "2023/03/20 (Mon) 10:35"],
    "haystack_sessions": [
        [
            {"role": "user", "content": "I need a new tire for my commuter bike this month."},
            {"role": "assistant", "content": "A bike shop can fit one."},
        ],
        [{"role": "user", "content": "My road bike runs great. I own two bikes now."}],
    ],
}


def test_the_stand_in_posts_one_record_per_mention_of_the_kind() -> None:
    records = offline.standin_records(QUESTION, "bike")

    assert [(r.thing, r.quantity, r.day) for r in records] == [
        ("commuter bike", None, "2023-03-20"),
        ("road bike", None, "2023-03-20"),
        ("own two bike", 2.0, "2023-03-20"),
    ]


def test_the_figure_is_the_stated_quantity_when_the_person_gave_one() -> None:
    count, quantity_sum = offline.counted(QUESTION, "bike")

    assert (count.things, quantity_sum, offline.figure(count, quantity_sum)) == (3, 2.0, 2.0)


def test_the_verdict_names_what_the_ledger_would_have_done() -> None:
    count, quantity_sum = offline.counted(QUESTION, "bike")
    answer = offline.figure(count, quantity_sum)

    outcomes = (
        offline.verdict(False, 2.0, count, answer),
        offline.verdict(True, 2.0, count, answer),
        offline.verdict(True, 3.0, count, answer),
        offline.verdict(False, None, count, answer),
    )

    assert outcomes == ("fixed", "kept", "broke", "out_of_scope")


def test_gold_and_stated_numbers_are_read_in_digits_and_words_and_never_as_a_year() -> None:
    numbers = (
        offline.gold_number("$3,750"),
        offline.gold_number("I attended four movie festivals."),
        offline.stated_number("In 2023 the user attended 5 weddings"),
        offline.stated_number("nothing numeric"),
    )

    assert numbers == (3750.0, 4.0, 5.0, None)


def test_the_split_reads_the_question_id_and_never_its_type() -> None:
    splits = {offline.split_of(f"q{index}") for index in range(40)}

    assert (splits, offline.split_of("conv-3_12"), offline.split_of("conv-41_62")) == (
        {"tune", "decide"},
        "tune",
        "decide",
    )
