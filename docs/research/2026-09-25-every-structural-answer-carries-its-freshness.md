# Every structural answer carries its freshness

Date: 2026-09-25. Audit item B-35 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- Only the graph modes of `get_architecture` added `_freshness_fields` (the generation's commit
  against the checkout's, and a refresh started when they differ). The summary and the modes
  `provenance`, `snippet`, `coverage`, `search`, `query`, `data_flow` and `cross_service` read the
  same generation and said nothing about its age, and started no refresh.
- With B-34, the envelope's `graph` component now follows that block, so an answer without it is
  reported `unknown`.

## Source

- RFC 9111, https://www.rfc-editor.org/rfc/rfc9111 (fetched 2026-09-25 for B-34), section 4.2:
  "A 'fresh' response is one whose age has not yet exceeded its freshness lifetime." An answer
  that cannot say its age cannot be called fresh.

## Decision

- One helper, `_with_generation_freshness`, adds the block (and so the refresh) to the summary and
  to every directory-checked mode; an answer with an error or with its own block is left as it is.

## Files

- `scripts/mcp_server.py`
- `tests/test_every_structural_answer_carries_its_freshness.py`
- `CHANGELOG.md`
