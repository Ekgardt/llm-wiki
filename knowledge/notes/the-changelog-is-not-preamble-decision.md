---
type: decision
status: accepted
confidence: high
source_authority: user
created: 2026-08-29
---

# The Changelog Is Not Preamble

One-sentence summary: `knowledge/log.md` is no longer imported into every
session, because an append-only changelog grows without bound and had reached
76,000 tokens of fixed cost per session — which stopped being a price and
became a wall when three agent runs died on it in a row.

## Decision

`CLAUDE.md` and `AGENTS.md` import `@knowledge/index.md` only. The log stays
tracked, stays append-only, and rule 4 of section 3 still requires appending to
it on every important update. It is read on demand — by `grep`, by the search
tools, by anyone who wants to know why something was done.

## Why

Measured 2026-08-29. The `@` directive pulls a file in whole, and the imported
set had reached 344,088 bytes: `knowledge/log.md` 304,980, `CLAUDE.md` 23,134,
`knowledge/index.md` 15,974 — about 86,000 tokens before a session does
anything at all.

The growth is measured, not estimated: at the `v4.0.0` tag the log was 147,507
bytes, thirty commits ago 259,477, and 304,980 now. It doubled after the
release, and most of that arrived in a single day of twenty commits.

The consequence was not theoretical. Three tasks in a row — finishing the
TypeScript path, `NEW-138`, and three reliability gaps — never started. Each
died at its first step with `Prompt is too long`.

Rule 4 requires a system that uses an LLM to spend tokens sparingly. Paying
86,000 tokens per session for the vault's own changelog is the opposite, and
the number grew linearly with the vault's work under no bound at all, so the
question was never whether it would break but when.

The log is not operating context. Its own footer calls it "an append-only
editorial changelog of compile passes and hygiene actions". The index is
different: it is the map of pages, which is what a preamble is for.

## What was rejected

Importing a tail of the log. `@` takes a file whole, so a tail would need a
second, generated file — another writer and another source of drift, for
context that one `grep` already returns.

## Evidence

Preamble 344,088 bytes to 39,688 — about 86,000 tokens to 9,900, a factor of
8.7. `AGENTS.md` stays byte-identical, pinned by
`tests/test_structure.py`; 59 tests across the structure and README suites
pass.

## Source

- `docs/research/2026-08-29-what-belongs-in-every-session.md`
- `CLAUDE.md`, `AGENTS.md` (section 3, Special files)

## Related

- [[knowledge/notes/oversized-daily-compile-decision]] — the same shape one
  layer down: a file that grows with the vault's work until it no longer fits
  the budget that has to read it.
