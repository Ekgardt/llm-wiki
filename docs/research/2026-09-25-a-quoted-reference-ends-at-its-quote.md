# A quoted evidence reference ends at its quote

Date: 2026-09-25. Audit item A-1 of `docs/AUDIT-2026-09-25-full.md`.

## Question

`read_page` over MCP answers "Evidence resolution failed: operation_failed" for 52
of the 209 notes on the live vault. Why, and what should the reference extractor
accept?

## Sources

- RFC 8259, "The JavaScript Object Notation (JSON) Data Interchange Format",
  section 7 (fetched 2026-09-25, https://www.rfc-editor.org/rfc/rfc8259#section-7):
  a string begins and ends with a quotation mark; a quotation mark inside it is
  escaped. A token that starts right after an unescaped `"` ends at the next one.
- The extractor's own contract (`extract_evidence_references`, since commit
  `79d18a25`, 2026-07-14): every `daily:` candidate is parsed, and a malformed one
  is an error rather than skipped.

## Findings (facts)

1. The compiler writes a `## Claims` block whose JSON carries
   `"evidence":{"reference":"daily:<day> sha256:<hex> block:<id> bytes:<a>-<b>",…}`;
   51 live notes hold one.
2. `evidence_resolver._reference_candidate` ends a candidate at a closing backtick
   when it opens with one, and otherwise takes the rest of the line. After `"` the
   rest of the line is `…bytes:1908-2248","sha256":"…`, which `EvidenceRef.parse`
   rejects, and `extract_evidence_references` raises for the whole page.
3. The same page's human evidence line, `` `daily:… bytes:1908-2248` ``, parses.

## Decision (conclusion)

A reference opened by a backtick or a quotation mark ends at the next matching
delimiter; one opened by neither still runs to the end of the line. The strictness
stays: a quoted reference that does not parse is still an error. The claims
block's references are real evidence and are resolved like the others.

## Edited files

- `scripts/evidence_resolver.py`
- `tests/test_a_quoted_reference_ends_at_its_quote.py` (new)
