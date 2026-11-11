"""Real fact-key turns retain their provenance through the public ledger entry.

Pure store/count/projection tests live in tests/modules/ledger_core.
Mechanism 2 of the plan approved on 2026-09-22: counting fails on an incomplete
set, so "how many" is counted over every record instead of by the model over
twelve chunks. Research:
`docs/research/2026-09-22-a-ledger-of-things-and-events-posted-once.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ledger  # noqa: E402
from fact_keys import Turn  # noqa: E402

TURN = Turn("knowledge/daily/2023-03-20.md", 100, 500, "a" * 64, "my road bike", "b" * 64)


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
