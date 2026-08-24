---
type: decision
title: "What Delimits One Daily Entry"
description: "A daily entry starts at a timestamp heading or at an operation marker, and evidence binds to the single entry that declares its timestamp."
date: 2026-08-21
confidence: high
source_authority: user
status: superseded
superseded_by: "[[daily-entry-quote-anchor-decision]]"
---
# What Delimits One Daily Entry

One-sentence summary: A daily entry starts at a timestamp heading or at an operation marker, and evidence binds to the single entry that declares its timestamp.

## Decision

Date: 2026-08-21.

An **entry** in a daily log begins at either delimiter the product writes:

- a `## [HH:MM:SS] …` heading, written by flush, session-end and the MCP
  decision tool; or
- an `<!-- llm-wiki-operation:<digest> -->` marker, written by the lifecycle
  capture through `daily_log_append.locked_append_once`.

An entry ends where the next entry begins, whichever delimiter starts it.

Each entry **declares** one timestamp: a heading entry in its heading, a marker
entry at the head of its first content line, which is where the capture writers
already put it (`` - `[HH:MM:SS] prompt | …` ``).

Compile evidence names a timestamp. Exactly one entry must declare it. Zero
entries and two entries are both refused, with the message unchanged:
`compile evidence timestamp block is ambiguous or missing`.

Everything else the evidence contract guaranteed is unchanged. The quote must
appear exactly once inside that one entry, and it must be a complete line, with
a bullet prefix stripped.

## Why

The binder recognised only the heading. The daily log this vault's own runtime
wrote on 2026-08-21 contains 295 entries and **zero** headings, so nothing the
lifecycle capture records could be cited by a compiled page. The compiler could
read those days; it could not prove anything from them.

## What was considered and rejected

**A tolerant reader** — accept anything entry-shaped. Rejected. Every write-up
that recommends the pattern also names its price: silent failure. Here the thing
being parsed is the proof that a claim came from a real line of a real session,
so a boundary guessed wrong produces a wrong attribution that the receipt still
calls resolved.

**Making the capture writers emit headings instead.** Rejected: it would not
help the logs already written, and the marker is load-bearing — it is what makes
capture idempotent.

Thomson's argument against the robustness principle decided the shape. Tolerance
is for frozen specifications between strangers; where the specification can
still be maintained, tolerance entrenches errors and lets implementations
diverge. Both sides here ship from one repository, so the answer is to state the
contract and keep the consumer strict about it.

## Where the definition lives

`evidence_resolver.daily_entries` is the one definition. The evidence resolver,
the archive packager (`archive_daily`), the claim pipeline
(`claims.split_blocks`) and the compile binder all read entries from it.

That unification was the operator's decision on 2026-08-21, taken after the
first attempt fixed only the compile binder and left captured entries still
uncitable: `EvidenceResolver.resolve_bytes` re-resolves every reference and held
its own heading-only copy, and `claims.split_blocks` held a third. Three answers
to one question is the divergence the research note warns about; there is now
one.

A captured entry declares its id only when its first content line begins with an
`[HH:MM:SS]`, which is what the capture writers write. An entry that declares no
usable id is not citable and is left out rather than guessed at.

## What this deliberately does not do

`_daily_entry_offsets`, which splits an oversized day into compilable parts,
still splits only on the marker. Part boundaries decide the digest a compile
receipt is written under, so moving them is a migration rather than a parser
fix, and it needs its own decision. When the splitter is migrated it should
consult `daily_entries` rather than grow a fourth answer.

## One behaviour is stricter

A heading entry used to run to the next heading, swallowing any marker entries
after it, so a quote from one of those could be attributed to the heading. An
entry now ends at the next delimiter of either kind.

## Evidence

- `scripts/evidence_resolver.py` — `daily_entries`, `_entry_starts`,
  `_marker_entry_id`; `scripts/compile_memory.py` — `_evidence_block`;
  `scripts/archive_daily.py` and `scripts/claims.py` — the two other readers.
- `tests/test_daily_entry_boundary.py` — ten cases, including one that binds a
  captured entry end to end through `_validate_semantic_operation`, which is the
  case that caught the resolver holding a second definition. The other nine: a
  captured entry binds, a
  heading entry still binds, a heading stops at the next captured entry, a
  same-second collision is refused, an undeclared timestamp is refused, a log
  with no entries is refused, an absent source is refused, and the complete-line
  rule holds for a captured bullet.
- `docs/research/2026-08-21-daily-entry-boundary.md` — the sources and what each
  rules in or out.

## Related

- [[knowledge/notes/oversized-daily-compile-decision|Oversized Daily Compile]] — the other decision about where a daily log may be cut.
- [[knowledge/notes/reliable-memory-stage-2|Reliable Memory Stage 2]] — the transaction and receipt contract this evidence feeds.
- [[knowledge/notes/citation-relevance-gate-decision|Citation Relevance Gate]] — the other gate that decides whether a citation counts.
