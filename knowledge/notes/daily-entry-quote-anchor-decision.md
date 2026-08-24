---
type: decision
title: "A Daily Entry Is Addressed By Timestamp And Proved By Quote"
description: "A daily entry starts at a timestamp heading or an operation marker; when several entries share a timestamp, the quote decides which one the evidence binds to."
date: 2026-08-24
confidence: high
source_authority: user
status: active
---
# A Daily Entry Is Addressed By Timestamp And Proved By Quote

One-sentence summary: A daily entry starts at a timestamp heading or an operation marker, and evidence binds to the entry that contains its quote — the timestamp narrows the search, the quote settles it.

## Decision

Date: 2026-08-24. Supersedes [[knowledge/notes/daily-entry-boundary-decision]],
which this page restates in full and refines in one clause.

An **entry** in a daily log begins at either delimiter the product writes:

- a `## [HH:MM:SS] …` heading, written by flush, session-end and the MCP
  decision tool; or
- an `<!-- llm-wiki-operation:<digest> -->` marker, written by the lifecycle
  capture through `daily_log_append.locked_append_once`.

An entry ends where the next entry begins, whichever delimiter starts it. Each
entry **declares** one timestamp: a heading entry in its heading, a marker entry
at the head of its first content line, which is where the capture writers already
put it (`` - `[HH:MM:SS] prompt | …` ``).

Compile evidence names a timestamp. **The timestamp selects the candidate
entries; the quote selects among them.**

- No entry declares the timestamp → refused, message unchanged.
- Exactly one entry declares it → that entry, exactly as before.
- Several entries declare it → the one that contains the quote exactly once. If
  none of them contains it, or more than one does, the evidence is refused with
  the same message.

Everything else the evidence contract guaranteed is unchanged. The quote must
appear exactly once inside the chosen entry, it must be a complete line with a
bullet prefix stripped, and the span is still bound by digest to the immutable
snapshot. `evidence_resolver.daily_entries` remains the one definition of an
entry, and the oversized-day splitter still splits only on the marker.

## Why

The refined clause used to say that two entries declaring one timestamp are
refused. That treated the address as the proof.

Measured on 2026-08-24: consolidating the imported history wrote 358 entries into
one daily log, and because the batches of a day shared a single moment, twelve of
them carry `[13:25:41]`, three `[13:24:10]`, two `[13:22:53]`. The writer is
fixed, but a daily log is append-only, so those entries stay as they are — and
every compile of that day failed as soon as the model cited one of them, discarding
126 durable items that each carried a verified verbatim quote.

The practice separates the two roles this rule had conflated. In the W3C Web
Annotation model a `TextPositionSelector` says where a selection sits and a
`TextQuoteSelector` says what it says; implementations write both and treat the
quote as the durable anchor, re-anchoring by quote when the position no longer
matches. The one case that needs more than a quote is a document that repeats the
same passage — which is exactly the case still refused here.

Research: `docs/research/2026-08-24-the-quote-is-the-anchor.md`.

## What this deliberately does not do

It does not weaken any check on the quote. A quote that appears in two candidate
entries is refused, because then nothing says which line was meant, and a wrong
attribution that the receipt calls resolved is the failure this whole contract
exists to prevent.

It does not let an entry be cited without a declared timestamp, and it does not
change the splitter, the receipts, or the digest binding.

## Source / Evidence

- `scripts/compile_memory.py::_evidence_block` — where the rule lives.
- `tests/test_daily_entry_boundary.py` — the boundary contract.
- `knowledge/daily/2026-08-24.md` — the 358 entries and the three shared timestamps.
- `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md` — NEW-75.

## Related

- [[knowledge/notes/daily-entry-boundary-decision]] — superseded by this page.
- [[knowledge/notes/oversized-daily-compile-decision]] — the splitter this does not touch.
- [[knowledge/notes/citation-relevance-gate-decision]] — the other half of what a citation must satisfy.
