"""Quote, then answer: the citation check is a string match on the quote.

The model copies the words of the evidence that carry a claim before it
writes the claim; the gate then asks only whether those words occur in the
cited span. A model asked "does this citation support the claim" is wrong
about one time in five (AttributionBench, ~80% macro-F1); a substring test is
not. A claim that wrote no quotes keeps the older gates, so a provider that
ignores the instruction is verified as before.
See `docs/research/2026-09-22-quote-then-answer-and-a-refusal-calibrated-on-twins.md`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from query_memory import ANSWER_SCHEMA, _claim_survives, _qa_system_prompt  # noqa: E402
from reliable_memory import validate_schema  # noqa: E402

SPAN = "On Tuesday my grandma gave me her silver necklace; I was 12 then and it cost her $200."
SUPPLIED = {"E1": {"text": SPAN, "relative_path": "knowledge/daily/2023-05-16.md"}}
CITED = {"E1": {"citation_id": "E1"}}


def _claim(text: str, quotes: list[str] | None = None, **fields: object) -> dict:
    claim = {"text": text, "citation_ids": ["E1"], **fields}
    if quotes is not None:
        claim["quotes"] = quotes
    return claim


def test_a_verbatim_quote_carries_a_claim_that_shares_no_word_with_the_span() -> None:
    """The overlap gate would have refused this claim; the quote is the support."""
    kept = _claim_survives(_claim("Twelve years old.", ["I was 12 then"]), CITED, SUPPLIED)

    assert kept == {"E1"}


def test_a_quote_is_matched_after_whitespace_and_case_are_folded() -> None:
    kept = _claim_survives(_claim("Twelve years old.", ["i WAS   12\nthen"]), CITED, SUPPLIED)

    assert kept == {"E1"}


def test_a_quote_the_evidence_does_not_contain_drops_the_claim() -> None:
    verdict = _claim_survives(_claim("She was 12.", ["my aunt gave me the necklace"]), CITED, SUPPLIED)

    assert verdict == "a quote is not in the cited evidence"


def test_a_claim_stating_a_figure_its_quote_does_not_carry_is_dropped() -> None:
    """The figure gate now reads the quote, not the whole span: 200 is in the span, not the quote."""
    verdict = _claim_survives(_claim("The necklace cost $300.", ["gave me her silver necklace"]), CITED, SUPPLIED)

    assert verdict == "cited span states different figures than the claim it is offered for"


def test_a_claim_without_quotes_keeps_the_older_gates() -> None:
    dropped = _claim_survives(_claim("The car was red."), CITED, SUPPLIED)
    kept = _claim_survives(_claim("The necklace was silver."), CITED, SUPPLIED)

    assert dropped == "cited evidence shares no content with the claim it supports"
    assert kept == {"E1"}


def test_the_schema_takes_quotes_before_the_text_and_the_prompt_asks_for_them_first() -> None:
    schema = json.loads(ANSWER_SCHEMA.read_text(encoding="utf-8"))
    properties = list(schema["properties"]["claims"]["items"]["properties"])
    document = {
        "schema_version": "grounded-answer/v1",
        "status": "answered",
        "claims": [_claim("Twelve.", ["I was 12 then"])],
        "citations": [{"citation_id": "E1"}],
        "reason": None,
    }

    validate_schema(document, ANSWER_SCHEMA)
    assert properties.index("quotes") < properties.index("text")
    assert "copy into its quotes the exact words" in _qa_system_prompt()


SECOND_SPAN = "Congratulations on your silver necklace, it suits you!"
TWO_SPANS = {**SUPPLIED, "E2": {"text": SECOND_SPAN, "relative_path": "knowledge/daily/2023-05-17.md"}}
TWO_CITED = {**CITED, "E2": {"citation_id": "E2"}}


def _two_span_claim(text: str, quotes: list[str]) -> dict:
    return {"text": text, "citation_ids": ["E1", "E2"], "quotes": quotes}


def test_a_claim_quoting_each_of_its_two_spans_once_survives() -> None:
    """One quote per cited span is what the prompt asks for; no span must hold the other's quote."""
    claim = _two_span_claim("The necklace was silver.", ["her silver necklace", "on your silver necklace"])

    assert _claim_survives(claim, TWO_CITED, TWO_SPANS) == {"E1", "E2"}


def test_a_cited_span_none_of_the_quotes_comes_from_drops_the_claim() -> None:
    claim = _two_span_claim("The necklace was silver.", ["her silver necklace"])

    assert _claim_survives(claim, TWO_CITED, TWO_SPANS) == "a quote is not in the cited evidence"


def test_a_quote_from_no_cited_span_drops_a_two_span_claim() -> None:
    claim = _two_span_claim(
        "The necklace was silver.", ["her silver necklace", "on your silver necklace", "my aunt"]
    )

    assert _claim_survives(claim, TWO_CITED, TWO_SPANS) == "a quote is not in the cited evidence"


def test_a_figure_one_span_quotes_carries_a_two_span_claim() -> None:
    claim = _two_span_claim("The necklace cost $200.", ["it cost her $200", "your silver necklace"])

    assert _claim_survives(claim, TWO_CITED, TWO_SPANS) == {"E1", "E2"}
