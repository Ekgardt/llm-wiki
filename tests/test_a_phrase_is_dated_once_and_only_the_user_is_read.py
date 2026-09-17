"""One phrase gets one date, and an assistant's remark gets none.

Third audit, 2026-09-17: "the day before yesterday" was dated twice, the second
time a day off, and an entry holding only assistant turns was read whole.
See `docs/research/2026-09-17-a-phrase-is-dated-once-and-only-the-user-is-read.md`.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import temporal_anchor  # noqa: E402

ANCHOR = date(2023, 5, 31)


def test_the_day_before_yesterday_writes_one_calendar_line() -> None:
    entry = "### Session\n\n**user:** The day before yesterday I packed the boxes.\n"

    footer = temporal_anchor.annotation(entry, ANCHOR)

    assert [line[:12] for line in footer.splitlines() if line.startswith("- ")] == ["- 2023-05-29"]


def test_a_question_about_the_day_before_yesterday_carries_that_day_only() -> None:
    expanded = temporal_anchor.query_with_dates("What did I pack the day before yesterday?", ANCHOR)

    assert expanded.endswith("? 2023-05-29")


def test_both_phrases_in_one_entry_keep_their_own_days() -> None:
    found = temporal_anchor.resolutions("Yesterday I rested; the day before yesterday I packed.", ANCHOR)

    assert found == {"yesterday": "2023-05-30", "the day before yesterday": "2023-05-29"}


def test_an_entry_with_only_assistant_turns_is_a_conversation_and_dates_nothing() -> None:
    entry = "### Session (10:00)\n\n**assistant:** As I said yesterday, the build is green.\n"

    assert temporal_anchor.annotation(entry, ANCHOR) == ""
