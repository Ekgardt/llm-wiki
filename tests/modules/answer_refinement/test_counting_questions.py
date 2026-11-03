"""Original pure counting assertions, unchanged."""
from __future__ import annotations

import aggregation_pass

# The four the diagnosis of 2026-09-19 names, with the unit each one counts.
NAMED = (
    ("3a704032", "How many plants did I acquire in the last month?", "plant"),
    (
        "9ee3ecd6",
        "How many points do I need to earn to redeem a free skincare product at Sephora?",
        "point",
    ),
    ("67e0d0f2", "What is the total number of online courses I've completed?", "course"),
    ("8e91e7d9", "What is the total number of siblings I have?", "sibling"),
)
# Counting and summing questions taken verbatim from other recorded rows.
SAMPLE = (
    "How many weddings have I attended in this year?",
    "How much money did I raise for charity in total?",
    "How many bikes did I service or plan to service in March?",
    "What is the total amount I spent on luxury items in the past few months?",
    "What is the average age of me, my parents, and my grandparents?",
    "How many different types of citrus fruits have I used in my cocktail recipes?",
    "How many hours in total did I spend driving to my three road trip destinations combined?",
    "What is the total distance I covered in my four road trips?",
)
# Plain lookups: one fact, no derivation, nothing to aggregate.
LOOKUPS = (
    "What degree did I graduate with?",
    "Where did I order on Monday?",
    "What did the doctor say about my knee?",
    "When did I move into the new apartment?",
    "Which airline did I fly with in March?",
)


def test_the_four_questions_the_diagnosis_named_open_the_pass() -> None:
    read = tuple(
        (name, aggregation_pass.asks_to_aggregate(question), aggregation_pass.counted_kind(question))
        for name, question, _ in NAMED
    )

    assert read == tuple((name, True, kind) for name, _, kind in NAMED)


def test_a_representative_sample_of_counting_questions_opens_the_pass() -> None:
    assert [q for q in SAMPLE if not aggregation_pass.asks_to_aggregate(q)] == []


def test_a_plain_lookup_question_leaves_the_pass_shut() -> None:
    assert [q for q in LOOKUPS if aggregation_pass.asks_to_aggregate(q)] == []


def test_the_counting_rule_names_the_thing_being_counted() -> None:
    named = aggregation_pass.counting_rule("How many bikes did I service in March?")
    plain = aggregation_pass.counting_rule("What did the doctor say about my knee?")

    assert "Count bikes, not mentions or events" in named
    assert "not mentions or events" not in plain
    assert plain.startswith("<counting_rule>\n")


# --- what a count counts ---------------------------------------------------------

BIKES = (
    "A road bike serviced at Pedal Power on March 10",
    "Commuter bike front tire replaced before April",
    "The road bike chain cleaned and lubricated on March 2",
)


def _replies(groups: str):
    def cluster(prompt: str) -> str:
        cluster.prompts.append(prompt)
        return groups

    cluster.prompts = []  # type: ignore[attr-defined]
    return cluster


def test_two_mentions_of_one_thing_collapse_into_one() -> None:
    cluster = _replies('{"groups": [[0], [1, 2]]}')

    groups = aggregation_pass.duplicate_groups(BIKES, cluster, "bike")

    assert groups == [[BIKES[0], BIKES[2]]]


def test_two_different_things_are_never_collapsed() -> None:
    cluster = _replies('{"groups": [[0], [1], [2]]}')

    groups = aggregation_pass.duplicate_groups(BIKES, cluster, "bike")

    assert groups == []


def test_mentions_of_one_thing_share_a_clustering_call() -> None:
    """Blocking, not merging: the key decides the order, the model decides the groups."""
    cluster = _replies('{"groups": []}')

    aggregation_pass.duplicate_groups(BIKES, cluster, "bike")
    lines = cluster.prompts[0].splitlines()  # type: ignore[attr-defined]

    assert lines[0] == "Each group must name one and the same bike."
    # Plain alphabetical order would have put the commuter bike between the two
    # road bikes, because one of them opens "A" and the other "The".
    assert [line.split(": ", 1)[1] for line in lines[1:]] == [BIKES[1], BIKES[0], BIKES[2]]


def test_the_note_beside_the_question_names_the_unit() -> None:
    note = aggregation_pass.entity_note([["A road bike", "The road bike"]], "bike")

    assert "name one and the same bike" in note
    assert "name one and the same thing" in aggregation_pass.entity_note([["a", "b"]])
