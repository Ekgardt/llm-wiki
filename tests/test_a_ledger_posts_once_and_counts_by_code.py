"""A ledger record is posted once, merged by the same-event rule and counted by code.

Mechanism 2 of the plan approved on 2026-09-22: counting fails on an incomplete
set, so "how many" is counted over every record instead of by the model over
twelve chunks. Research:
`docs/research/2026-09-22-a-ledger-of-things-and-events-posted-once.md`.
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ledger  # noqa: E402
from fact_keys import Turn  # noqa: E402

TURN = Turn("knowledge/daily/2023-03-20.md", 100, 500, "a" * 64, "my road bike", "b" * 64)


def _record(thing: str, day: str, event: str = "serviced", **fields) -> ledger.Record:
    base = ledger.Record(
        kind="bike",
        thing=thing,
        event=event,
        day=day,
        quantity=None,
        by_user=True,
        dated=True,
        source_path=f"knowledge/daily/{day}.md",
        source_sha256="c" * 64,
        byte_start=10,
        byte_end=90,
        span_sha256="d" * 64,
    )
    return replace(base, **fields)


@pytest.fixture
def store() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    ledger.ensure_table(connection)
    return connection


def test_a_reply_record_is_read_into_canonical_forms() -> None:
    value = {
        "facts": ["I serviced my road bike"],
        "records": [
            {"kind": "Bikes", "thing": "my Road Bike", "event": "serviced", "date": "2023-03-10", "quantity": 1},
            {"kind": "", "thing": "nothing"},
            "not a record",
        ],
    }

    found = ledger.records_of(TURN, value)

    assert [(r.kind, r.thing, r.event, r.day, r.quantity, r.dated) for r in found] == [
        ("bike", "road bike", "serviced", "2023-03-10", 1.0, True)
    ]


def test_a_record_without_a_stated_date_takes_the_day_of_its_daily_file() -> None:
    found = ledger.records_of(TURN, {"records": [{"kind": "bike", "thing": "commuter bike"}]})

    assert (found[0].day, found[0].dated, found[0].source_sha256) == ("2023-03-20", False, "b" * 64)


def test_a_list_shaped_reply_carries_no_records() -> None:
    assert ledger.records_of(TURN, ["I own a road bike"]) == []


def test_the_same_record_is_posted_once(store: sqlite3.Connection) -> None:
    record = _record("road bike", "2023-03-10")

    first = ledger.post(store, [record, record])
    second = ledger.post(store, [record])

    assert (first, second, len(ledger.rows(store))) == (1, 0, 1)


def _march_bikes(store: sqlite3.Connection) -> None:
    ledger.post(
        store,
        [
            _record("road bike", "2023-03-02", byte_start=1),
            _record("my road bike", "2023-03-10", byte_start=2),
            _record("commuter bike", "2023-03-15", "front tire replaced", byte_start=3),
            _record("road bike", "2023-06-01", byte_start=4),
        ],
    )


def test_two_mentions_of_one_thing_within_the_window_are_one_event(store: sqlite3.Connection) -> None:
    _march_bikes(store)

    march = ledger.count(store, "bikes", ("2023-03-01", "2023-03-31"))

    assert (march.records, march.events, march.things) == (3, 2, 2)


def test_a_mention_outside_the_window_is_a_new_event_and_not_a_new_thing(store: sqlite3.Connection) -> None:
    _march_bikes(store)

    whole = ledger.count(store, "bike")

    assert (whole.records, whole.events, whole.things, whole.tier) == (4, 3, 2, ledger.CONFIRMED)


def test_a_different_stated_quantity_is_evidence_of_a_new_event() -> None:
    same = _record("chili plant", "2023-05-01", "planted", quantity=5.0)
    other = replace(same, quantity=2.0, byte_start=99)

    assert (ledger.same_event(same, replace(same, byte_start=98)), ledger.same_event(same, other)) == (
        True,
        False,
    )


def test_a_record_the_user_never_dated_makes_the_count_probable(store: sqlite3.Connection) -> None:
    ledger.post(store, [_record("road bike", "2023-03-02", dated=False)])

    counted = ledger.count(store, "bike")

    assert (counted.things, counted.tier) == (1, ledger.PROBABLE)


def test_reconcile_flags_a_number_the_ledger_does_not_hold(store: sqlite3.Connection) -> None:
    _march_bikes(store)
    counted = ledger.count(store, "bike", ("2023-03-01", "2023-03-31"))

    agreed = ledger.reconcile(2, counted)
    flagged = ledger.reconcile(4, counted)
    unknown = ledger.reconcile(None, counted)

    assert (agreed.agrees, agreed.unit, flagged.agrees, unknown.agrees) == (True, "things", False, None)


def test_a_generation_without_a_ledger_answers_none_not_zero() -> None:
    bare = sqlite3.connect(":memory:")

    assert (ledger.count(bare, "bike"), ledger.reconcile(3, None).agrees) == (None, None)


def test_the_recurrence_gate_opens_on_the_second_day(store: sqlite3.Connection) -> None:
    _march_bikes(store)

    seen_again = ledger.recurring(store)

    assert [(item.thing, item.days) for item in seen_again] == [
        ("road bike", ("2023-03-02", "2023-03-10", "2023-06-01"))
    ]


def test_an_entity_page_is_extended_with_dated_pointers_and_never_twice(store: sqlite3.Connection) -> None:
    _march_bikes(store)
    recurrence = ledger.recurring(store)[0]

    created = ledger.entity_page_bytes(None, recurrence, "2023-06-02")
    again = ledger.entity_page_bytes(created, recurrence, "2023-06-03")
    text = created.decode("utf-8")

    assert text.startswith("---\ntype: entity\n")
    assert text.count("- 2023-0") == 3
    assert ("bytes 1-90" in text, again) == (True, None)


def test_a_page_written_by_hand_gets_the_ledger_section_appended(store: sqlite3.Connection) -> None:
    _march_bikes(store)
    recurrence = ledger.recurring(store)[0]
    page = b"---\ntype: entity\n---\n# Road bike\n\nBought in 2021.\n"

    extended = ledger.entity_page_bytes(page, recurrence, "2023-06-02").decode("utf-8")

    assert extended.startswith(page.decode("utf-8"))
    assert extended.count(ledger.LEDGER_SECTION) == 1
