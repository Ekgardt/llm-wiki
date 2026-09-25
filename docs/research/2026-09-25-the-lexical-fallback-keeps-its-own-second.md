# The lexical fallback keeps its own second

Date: 2026-09-25. Audit item B-18 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and measured read-only on the live vault)

- `mcp_server._search_vault` runs the hybrid search to the operation deadline;
  on `TimeoutError` it runs one lexical pass, `_lexical_after_deadline`, with
  the same deadline. That deadline has already passed, so the fallback raises at
  once and the caller gets a timeout instead of the lexical answer the code
  promised.
- A lexical-only search (`semantic=False, graph=False, rerank=False`) on the live
  vault took 0.13–0.19 s in-process for three questions (0.23–0.35 s with
  interpreter start).

## Source

- Google SRE book, "Addressing Cascading Failures",
  https://sre.google/sre-book/addressing-cascading-failures/ (fetched
  2026-09-25): "You may want to reduce the outgoing deadline a bit (e.g., a few
  hundred milliseconds) to account for ... post-processing in the client", and
  "the server should check the deadline left at each stage before attempting to
  perform any more work". The fallback is that post-processing, and needs time
  left to run in.

## Decision

- The hybrid run gets the operation deadline less
  `LEXICAL_FALLBACK_RESERVE_SECONDS` = 1.0 s, five times the measured lexical
  pass; the fallback runs in that reserved second. When less than the reserve is
  left from the start, the lexical pass runs alone.

## Files

- `scripts/mcp_server.py`
- `tests/test_the_lexical_fallback_keeps_its_own_second.py`
- `CHANGELOG.md`
