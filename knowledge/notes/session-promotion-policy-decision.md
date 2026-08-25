---
type: decision
status: accepted
date: 2026-08-25
confidence: high
source_authority: user
---

# Retention is unconditional; promotion is decided by evidence, not by a tier

One-sentence summary: every session is kept regardless of what any classifier
thinks of it, and whether it becomes a compiled page is decided by the nightly
consolidation reading the whole record — so no number needs a human label
before the memory system can work.

## Context

`OPEN-034` asked how accurate the end-of-session tier classifier is, and
`NEW-62` measured something the labels could not explain: on forty real
sessions the product promoted **1** at the full input budget and **4** at a
third of it. The labels themselves proved unstable — the same forty sessions
redistributed 20/6/14 → 5/3/32 when only the excerpt length changed — so no
accuracy figure from that corpus could be published as fact, and the item sat
open waiting for a human to review labels.

Waiting is what made it wrong. The owner's standing rule is that the system
must work automatically without involving the user. A memory policy that
cannot proceed until someone hand-labels forty transcripts violates that rule
by construction, and it had already been overtaken: since
[[knowledge/notes/session-evidence-retention-decision]] (2026-08-23) every session is written
verbatim to `knowledge/raw/sessions/` **before** classification and regardless
of tier.

## Decision

1. **Retention is unconditional and is not a judgement.** The tier classifier
   does not decide whether a session is remembered. It cannot: the record is
   already on disk when it runs. This was true in code from 2026-08-23; it is
   now the written policy.
2. **Promotion is decided by consolidation, over the whole record.** The
   nightly pass reads the session records of the previous day, must quote a
   line that really exists in one of them for every durable item it proposes,
   and hands the result to the ordinary compile with its receipts and
   transactions. That path has evidence; the end-of-session hook does not.
3. **The end-of-session classifier keeps one job** — whether to append a daily
   entry now — and it reads the same head-and-tail sample the consolidation
   already uses, instead of the bare tail.
4. **`OPEN-034` stops being a gate.** The unstable labels were gating a number
   that no longer decides retention. `benchmark/run_flush_classification.py`
   stays as a regression check on the hint, and its accuracy figures stay
   marked provisional until a human confirms them — which is now optional.

## What this decision does not claim

- Not that head+tail is optimal. It is better than tail-only for a document
  that decides in its middle, and it makes one convention where there were two.
- Not that the classifier is accurate. That number is still unmeasured against
  trustworthy labels, and this decision removes the need to trust it, rather
  than the need to measure it.
- Not that nothing is ever lost. A session whose record fails to write is still
  lost; that path is covered by `record_capture_failure` and its counter, not
  by this decision.

## Consequences

- Nothing in the pipeline waits for the owner. Sessions accumulate, the nightly
  pass reads them, evidence-bearing items become pages.
- The cost of a wrong tier drops to one daily-log entry, from "this session is
  forgotten".
- `run/state.json` and the stand keep reporting tiers, so a regression in the
  hint is still visible.

## Source / Evidence

- Measurement: `NEW-62` in `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md` (1 of 40
  at full budget, 4 of 40 at a third).
- Code: `read_transcript_excerpt` in `scripts/flush_memory.py`;
  `_within_share` in `scripts/episode_consolidation.py`.
- Research: `docs/research/2026-08-25-what-the-vault-decides-to-remember.md`.
- Owner delegation: 2026-08-25, "прими решение используя правила", under the
  standing rule that the system must work without involving the user.

## Related

- [[knowledge/notes/session-evidence-retention-decision]]
- [[knowledge/notes/classification-measurement-stand-decision]]
- [[knowledge/notes/oversized-daily-compile-decision]]
