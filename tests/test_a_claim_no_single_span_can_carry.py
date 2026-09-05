"""Seven of eight wrong answers needed arithmetic the contract forbade.

Every atomic claim had to be carried by a cited span, and `_require_figures_agree`
refused a claim whose figures appear in none. A sum, a count, a gap between two
dates and a latest-of-several are in no span by construction, so the only move
the contract left was to quote an input — which is what the model did, and it was
wrong.

Measured on 50 questions, 2026-09-05: "a necklace that cost around $200" for a
question whose answer was $300; both dates for a question whose answer was the
gap between them; "just finished their third, currently on their fourth" for a
question whose answer was five.

A claim may now declare a derivation. Every input is still cited, resolved and
required to share words with the claim; only the output figure is exempt, and
only for a declared derivation over more than one span. The arithmetic itself is
not verified — it is made checkable, and this file says so.

See `docs/research/2026-09-05-a-claim-no-single-span-can-carry.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import query_memory  # noqa: E402
from evidence_resolver import EvidenceResolutionError  # noqa: E402

CITED = {"E1": {}, "E2": {}}
SUPPLIED = {
    "E1": {"text": "The necklace from the jeweller cost $200."},
    "E2": {"text": "The scarf for my sister cost $100."},
}


def _claim(text: str, ids: list[str], derivation: object = None) -> dict:
    return {"text": text, "citation_ids": ids, "derivation": derivation}


def test_a_total_stated_by_no_span_is_allowed_when_it_is_declared() -> None:
    claim = _claim("Gifts for my sister cost $300 in total: $200 and $100.", ["E1", "E2"], "sum")

    assert query_memory._cited_ids_of_claim(claim, CITED, SUPPLIED) == {"E1", "E2"}


def test_the_same_total_undeclared_is_still_refused() -> None:
    """The figure gate is what it always was for a claim that does not declare."""
    claim = _claim("Gifts for my sister cost $300 in total.", ["E1", "E2"])

    with pytest.raises(query_memory.GroundedQAError, match="different figures"):
        query_memory._cited_ids_of_claim(claim, CITED, SUPPLIED)


def test_a_derivation_over_one_span_is_not_a_derivation() -> None:
    """Otherwise the exemption is a way to assert anything beside one citation."""
    claim = _claim("Gifts for my sister cost $300 in total.", ["E1"], "sum")

    with pytest.raises(query_memory.GroundedQAError, match="only one"):
        query_memory._cited_ids_of_claim(claim, CITED, SUPPLIED)


def test_a_derived_claim_still_has_to_share_words_with_every_input() -> None:
    supplied = {**SUPPLIED, "E2": {"text": "Тишина в совершенно ином предмете."}}
    claim = _claim("Gifts for my sister cost $300 in total.", ["E1", "E2"], "sum")

    with pytest.raises((query_memory.GroundedQAError, EvidenceResolutionError)):
        query_memory._cited_ids_of_claim(claim, CITED, supplied)


def test_a_derived_claim_still_has_to_cite_evidence_that_was_supplied() -> None:
    claim = _claim("Gifts cost $300 in total.", ["E1", "E9"], "sum")

    with pytest.raises(EvidenceResolutionError):
        query_memory._cited_ids_of_claim(claim, CITED, SUPPLIED)


@pytest.mark.parametrize("kind", query_memory.DERIVATIONS)
def test_every_named_derivation_is_accepted(kind: str) -> None:
    claim = _claim("Gifts for my sister cost $300, from $200 and $100.", ["E1", "E2"], kind)

    assert query_memory._cited_ids_of_claim(claim, CITED, SUPPLIED) == {"E1", "E2"}


def test_an_unknown_derivation_word_earns_no_exemption() -> None:
    """A closed set, so a made-up kind cannot switch the gate off."""
    claim = _claim("Gifts for my sister cost $300 in total.", ["E1", "E2"], "guessed")

    with pytest.raises(query_memory.GroundedQAError, match="different figures"):
        query_memory._cited_ids_of_claim(claim, CITED, SUPPLIED)


def test_the_schema_admits_only_the_named_derivations() -> None:
    import json

    schema = json.loads(query_memory.ANSWER_SCHEMA.read_text(encoding="utf-8"))
    allowed = schema["properties"]["claims"]["items"]["properties"]["derivation"]

    assert allowed["oneOf"][1]["enum"] == list(query_memory.DERIVATIONS)
