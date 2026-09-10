"""A gap between dates is computed by the calendar, not narrated by the model.

Run 1, 2026-09-08: "How many days ago did I attend a networking event?" was
answered with the event's date and no number; "how many weeks ago" for the
Nordstrom sale was refused because a gap to today has one span. Now a
difference claim carries its dates in `inputs`, one span is enough when the
other end is the day the question is asked, the calendar computes the gap,
and when the claim does not state it the answer is generated once more with
the figure beside the question.
See `docs/research/2026-09-08-the-calendar-does-the-arithmetic.md`.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import calendar_pass  # noqa: E402
from corpus_snapshot import collect_corpus  # noqa: E402
from query_memory import _asked_on, grounded_qa  # noqa: E402
from temporal_anchor import query_with_dates  # noqa: E402


def _claim(text: str, inputs: list[str]) -> dict:
    return {"text": text, "citation_ids": ["E1"], "derivation": "difference", "inputs": inputs}


def _answer(*claims: dict) -> dict:
    return {"status": "answered", "claims": list(claims)}


ASKED = date(2022, 4, 4)


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


def test_the_day_asked_is_read_with_slashes_too() -> None:
    assert _asked_on("How long ago?\n(Current date: 2022/04/04 (Mon) 10:00)") == ASKED
    assert _asked_on("On 2022-04-04, how long ago?") == ASKED


def test_weeks_ago_in_a_question_reach_the_days_around_the_resolved_day() -> None:
    expanded = query_with_dates("What did I buy four weeks ago?", ASKED)

    assert "2022-03-07" in expanded
    assert "2022-03-04" in expanded and "2022-03-10" in expanded
    assert "2022-03-03" not in expanded
    assert "2022-03-31" not in query_with_dates("What did I buy three days ago?", ASKED)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge" / "notes").mkdir(parents=True)
    (root / "knowledge" / "daily").mkdir(parents=True)
    (root / "knowledge" / "notes" / "events.md").write_text(
        "---\ntype: concept\nsource_authority: user\nconfidence: high\n---\n\n"
        "# Events\n\nI attended the networking event on 2022-03-09 downtown.\n",
        encoding="utf-8",
    )
    return root


def _difference_answer(prompt: str, text: str) -> str:
    marker = "<evidence_manifest>\n"
    manifest = prompt.split(marker, 1)[1].split("\n</evidence_manifest>", 1)[0]
    evidence = json.loads(manifest)[0]
    citation = {key: evidence[key] for key in evidence if key != "text"}
    return json.dumps(
        {
            "schema_version": "grounded-answer/v1",
            "status": "answered",
            "claims": [
                {
                    "text": text,
                    "citation_ids": [evidence["citation_id"]],
                    "derivation": "difference",
                    "inputs": ["2022-03-09"],
                }
            ],
            "citations": [citation],
            "reason": None,
        }
    )


class _Model:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, prompt: str, system_prompt: str, max_tokens: int) -> str:
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            return _difference_answer(prompt, "You attended the networking event on 2022-03-09.")
        return _difference_answer(prompt, "The networking event was 26 days ago, on 2022-03-09.")


def test_a_gap_left_unstated_is_answered_again_with_the_calendar(vault: Path) -> None:
    snapshot = collect_corpus(vault)
    model = _Model()

    document = grounded_qa(
        "How many days ago did I attend a networking event?\n(Current date: 2022/04/04 (Mon) 10:00)",
        vault=vault,
        snapshot=snapshot,
        candidates=tuple(snapshot.chunks),
        generator=model,
        profile="BASE",
    )

    assert len(model.prompts) == 2
    assert "<calendar>" in model.prompts[1] and "26 days" in model.prompts[1]
    assert document["status"] == "answered"
    assert document["claims"][0]["text"].startswith("The networking event was 26 days ago")


def test_a_gap_with_one_span_passes_the_gate_and_a_stated_gap_is_final(vault: Path) -> None:
    snapshot = collect_corpus(vault)
    model = _Model()
    model.prompts.append("first pass already spent")  # so the model states the gap at once

    document = grounded_qa(
        "How many days ago did I attend a networking event?\n(Current date: 2022/04/04 (Mon) 10:00)",
        vault=vault,
        snapshot=snapshot,
        candidates=tuple(snapshot.chunks),
        generator=model,
        profile="BASE",
    )

    assert len(model.prompts) == 2
    assert document["status"] == "answered"
    assert len(document["claims"]) == 1
