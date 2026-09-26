# A page is read past one bad reference

Date: 2026-09-26. Audit 2026-09-26 B-17.

## Facts

- `mcp_server._page_evidence` parsed every `daily:` occurrence with
  `evidence_resolver.extract_evidence_references`, which raises on the first
  candidate that is not a canonical reference. A page that only mentions the
  prefix in prose (a backticked `daily:` in a sentence) came back as
  "Evidence resolution failed" with no content at all.
- The strict parser is right where references are written (compile validation):
  a writer must not produce one it cannot resolve.
- RFC 9413 (https://www.rfc-editor.org/rfc/rfc9413.html, fetched 2026-09-26) states
  the robustness principle as "Be strict when sending and tolerant when
  receiving", and also that choosing "to generate fatal errors for unspecified
  conditions instead of attempting error recovery can ensure that faults receive
  attention" — tolerance where input is merely unparseable, strictness where it
  is wrong.

## Decision

- `evidence_resolver.evidence_candidates` returns, per `daily:` occurrence, a
  parsed reference or a named reason; an occurrence that is the tail of another
  word is not a candidate. `read_page` lists an unparseable mention as
  `{"reference": null, "error": "not_an_evidence_reference"}` and returns the page.
- A reference that parses but does not resolve (wrong hash, missing day, too many
  bytes) still refuses the page, as before: that is a fault, not prose.
- `extract_evidence_references` stays strict for writers.

## Files

- `scripts/evidence_resolver.py`
- `scripts/mcp_server.py`
- `tests/test_a_page_is_read_past_one_bad_reference.py`
- `tests/test_mcp_server.py`
- `CHANGELOG.md`
