"""A quoted evidence reference ends at its quote, and lint reads the page read_page reads.

The `## Claims` block writes its references inside JSON strings; the extractor took the
rest of the line after the opening quotation mark and refused 52 of 209 live pages,
while lint skipped that block and said nothing. See
docs/research/2026-09-25-a-quoted-reference-ends-at-its-quote.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from evidence_resolver import extract_evidence_references

ROOT = Path(__file__).resolve().parent.parent
REFERENCE = f"daily:2026-09-07 sha256:{'a' * 64} block:03:00:53 bytes:1908-2248"
CLAIMS_LINE = '{"claims":[{"evidence":{"reference":"' + REFERENCE + '","sha256":"' + "b" * 64 + '"}}]}'


def test_a_reference_inside_a_json_string_ends_at_its_closing_quote() -> None:
    references = extract_evidence_references(CLAIMS_LINE)

    assert [reference.daily_id for reference in references] == ["2026-09-07"]


def test_prose_and_claims_references_parse_alike() -> None:
    page = f"- `{REFERENCE}` — the quote\n\n## Claims\n\n```json\n{CLAIMS_LINE}\n```\n"

    assert len(extract_evidence_references(page)) == 2


def test_a_quoted_reference_that_does_not_parse_is_still_an_error() -> None:
    with pytest.raises(ValueError, match="not canonical"):
        extract_evidence_references('{"reference":"daily:... bytes:..."}')


def test_lint_scans_the_whole_page_like_read_page() -> None:
    source = (ROOT / "scripts" / "lint_memory.py").read_text(encoding="utf-8")

    assert "CLAIMS_SECTION_RE" not in source
