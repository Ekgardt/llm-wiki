"""A reply is refused for what it asserts, not for a list beside its claims.

Four stand abstentions had a reason, no claim, and cited the spans that
conflicted; one answer had five cited claims and no `citations` list. All five
became errors. Research:
`docs/research/2026-09-14-a-list-the-claims-already-carry.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import query_memory  # noqa: E402
from query_memory import GroundedContext, GroundedQAError  # noqa: E402


def _document(**fields) -> dict:
    document = {"schema_version": "grounded-answer/v1", "reason": None, "claims": []}
    document.update(fields)
    return document


def test_an_abstention_that_cites_the_conflict_is_an_abstention(tmp_path):
    conflict = _document(
        status="conflicting_evidence",
        reason="Two follower counts on the same date.",
        citations=[{"citation_id": "E1"}, {"citation_id": "E2"}],
    )

    verified = query_memory.verify_grounded_answer(
        conflict, GroundedContext.empty(profile="default"), vault=tmp_path
    )

    assert (verified["status"], verified["citations"]) == ("conflicting_evidence", [])


def test_an_abstention_with_a_claim_is_still_refused(tmp_path):
    claimed = _document(
        status="insufficient_evidence",
        reason="nothing held",
        claims=[{"text": "It was Tuesday.", "citation_ids": ["E1"]}],
        citations=[],
    )

    with pytest.raises(GroundedQAError, match="abstention statuses"):
        query_memory.verify_grounded_answer(
            claimed, GroundedContext.empty(profile="default"), vault=tmp_path
        )


def test_an_answer_without_its_list_gets_the_ids_its_claims_carry():
    claims = [
        {"text": "It has four bays.", "citation_ids": ["E4", "E2"]},
        {"text": "It costs $300.", "citation_ids": ["E2"]},
    ]

    validated = query_memory._validated_answer_document(
        _document(status="answered", claims=claims)
    )

    assert validated["citations"] == [{"citation_id": "E4"}, {"citation_id": "E2"}]


def test_a_list_that_was_given_is_left_for_the_gates():
    given = _document(
        status="answered",
        claims=[{"text": "It has four bays.", "citation_ids": ["E4"]}],
        citations=[{"citation_id": "E9"}],
    )

    assert query_memory._validated_answer_document(given)["citations"] == [{"citation_id": "E9"}]
