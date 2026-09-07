"""A complete answer must not be destroyed for a note written beside it.

`reason` is where an abstention states itself, and the contract used to refuse
any answered reply that filled it. Measured over 200 questions on 2026-09-07:
twenty complete answers — claims, citations and all — were thrown away for
writing a note there, of the form "The 2023-09-30 entry states the count
directly. Note that other entries mention an Alex in unrelated contexts." Six
of the twenty had the gold answer in the prompt.

See `docs/research/2026-09-07-a-note-is-not-a-refusal.md`.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import query_memory  # noqa: E402
from query_memory import GroundedQAError  # noqa: E402


def _answered(**overrides):
    document = {
        "schema_version": "grounded-answer/v1",
        "status": "answered",
        "claims": [
            {"text": "It was on Tuesday.", "citation_ids": ["E1"], "derivation": None}
        ],
        "citations": [{"citation_id": "E1"}],
        "reason": None,
    }
    document.update(overrides)
    return document


def test_a_note_beside_a_grounded_answer_is_accepted():
    query_memory._require_answered_shape(
        _answered(reason="The 2023-09-30 entry states the count directly.")
    )


def test_a_reply_with_no_claims_is_still_refused():
    with pytest.raises(GroundedQAError, match="claims with citations"):
        query_memory._require_answered_shape(_answered(claims=[]))


def test_a_reply_with_no_citations_is_still_refused():
    with pytest.raises(GroundedQAError, match="claims with citations"):
        query_memory._require_answered_shape(_answered(citations=[]))


def test_an_abstention_still_needs_a_reason_and_no_claims():
    with pytest.raises(GroundedQAError, match="abstention statuses"):
        query_memory._require_abstention_shape(
            {
                "status": "insufficient_evidence",
                "claims": [],
                "citations": [],
                "reason": "",
            }
        )
    with pytest.raises(GroundedQAError, match="abstention statuses"):
        query_memory._require_abstention_shape(
            {
                "status": "insufficient_evidence",
                "claims": [{"text": "x"}],
                "citations": [],
                "reason": "nothing held",
            }
        )


def test_the_note_does_not_reach_the_reader_as_a_reason(monkeypatch):
    """The published answer states no reason, because it was not refused."""
    validated = _answered(reason="a note the model wrote beside its claims")
    monkeypatch.setattr(
        query_memory,
        "_kept_claims",
        lambda claims, cited, supplied: (list(claims), {"E1"}, []),
    )

    answer = query_memory._answer_of_surviving_claims(
        validated, {"E1": {"citation_id": "E1"}}, {}
    )

    assert answer["status"] == "answered"
    assert answer["reason"] is None
    assert answer["claims"] == validated["claims"]


def test_when_no_claim_survives_it_is_still_an_abstention(monkeypatch):
    monkeypatch.setattr(
        query_memory,
        "_kept_claims",
        lambda claims, cited, supplied: ([], set(), ["gate"]),
    )

    answer = query_memory._answer_of_surviving_claims(_answered(), {}, {})

    assert answer["status"] == "insufficient_evidence"
    assert answer["claims"] == []
    assert answer["reason"]
