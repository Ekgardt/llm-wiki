"""A gate that drops a claim must say so, even when the answer still goes out.

The refusal telemetry recorded total refusals only, and said so in its own
docstring. That blind spot hid the largest measured defect of the week: across
three runs of 200 questions, 176 of 399 answered questions had a claim dropped
and the answer published anyway, and those are wrong 23.3% of the time against
3.1% where nothing was dropped. Finding it took an afternoon of reading raw
replies by hand, which is the exact cost this telemetry exists to remove.

See `docs/research/2026-09-07-a-dropped-claim-and-a-wrong-answer.md`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import query_memory  # noqa: E402

WHOLE = "no claim survived its citation gates: cited span states different figures"


def test_a_partial_drop_names_its_gates():
    answer = {
        "status": "answered",
        "reason": None,
        query_memory.DROPPED_GATES_KEY: [
            "cited span states different figures than the claim it is offered for"
        ],
    }

    assert query_memory._refused_gates(answer) == [
        "cited span states different figures than the claim it is offered for"
    ]


def test_a_total_refusal_still_names_its_gates():
    assert query_memory._refused_gates({"status": "insufficient_evidence", "reason": WHOLE}) == [
        "cited span states different figures"
    ]


def test_both_kinds_are_reported_together_without_repeats():
    answer = {
        "status": "insufficient_evidence",
        "reason": WHOLE,
        query_memory.DROPPED_GATES_KEY: [
            "cited span states different figures",
            "cited evidence shares no content with the claim it supports",
        ],
    }

    assert query_memory._refused_gates(answer) == [
        "cited evidence shares no content with the claim it supports",
        "cited span states different figures",
    ]


def test_an_answer_that_lost_nothing_names_no_gate():
    assert query_memory._refused_gates({"status": "answered", "reason": None}) == []


def test_a_surviving_answer_carries_the_gates_that_dropped_a_claim(monkeypatch):
    monkeypatch.setattr(
        query_memory,
        "_kept_claims",
        lambda claims, cited, supplied: ([claims[0]], {"E1"}, ["a gate that refused"]),
    )
    validated = {
        "status": "answered",
        "reason": None,
        "claims": [{"text": "kept"}, {"text": "dropped"}],
        "citations": [{"citation_id": "E1"}],
    }

    answer = query_memory._answer_of_surviving_claims(
        validated, {"E1": {"citation_id": "E1"}}, {}
    )

    assert answer[query_memory.DROPPED_GATES_KEY] == ["a gate that refused"]
    assert query_memory._refused_gates(answer) == ["a gate that refused"]


def test_the_key_is_a_channel_and_not_a_field_of_an_answer():
    """`grounded_qa` removes it, so no reader ever sees it."""
    source = Path(query_memory.__file__).read_text(encoding="utf-8")

    assert "answer.pop(DROPPED_GATES_KEY, None)" in source
