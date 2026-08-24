---
type: decision
title: "Evidence Names The Slice It Was Compiled From"
description: "A page's citation names the compile part it was written from, and a reader accepts it when that part's bytes are still present verbatim, entry-aligned, at a part boundary."
date: 2026-08-24
confidence: high
source_authority: user
status: active
---
# Evidence Names The Slice It Was Compiled From

One-sentence summary: A compiled page cites the bytes of the compile part it was written from, and every reader verifies that citation by finding an entry-aligned slice, starting where a part starts, whose bytes still hash to what the page recorded.

## Decision

Date: 2026-08-24.

A daily log longer than `MAX_DAILY_PART_BYTES` (16 KiB) is compiled in parts,
split at entry boundaries. A page compiled from a part cites **that part**: the
reference carries the part's digest and byte offsets inside the part, not inside
the day.

A reader resolves such a reference in this order:

1. the whole day, when its digest matches — the ordinary short day;
2. otherwise, an **entry-aligned slice that begins where a compile part begins**
   and whose bytes hash to the recorded digest;
3. otherwise the citation fails, exactly as before.

Rule 2 covers both real cases: the part as it was compiled, and a part that was
the tail of an open day then and is the head of a longer part now. Nothing
weaker is accepted — the historical bytes must still be present verbatim and in
place, so an edit inside the cited region still fails the citation, and so does
a reordered or rewritten day.

The splitter now lives in `scripts/evidence_resolver.py`, next to the reader,
and `scripts/compile_memory.py` imports it from there.

## Why

Measured on this vault on 2026-08-24: 31 of 99 pages failed their own citation
with `flat daily source hash mismatch`. Every one of the 31 digests was a part
digest. The writer emitted part-scoped evidence and validated it against the
part in memory; every later reader — lint, the answer path, the archive — knew
only how to check a whole file. Both sides were tested and both were right on
their own terms; nothing tested them against each other, so the vault's own
knowledge failed the vault's own check the moment its days grew past 16 KiB,
which is every day here.

A whole-file digest is the wrong unit for a file that keeps growing: it fails on
every legitimate append, and a check that fails on legitimate change is a check
people learn to ignore. The unit of verification has to be the unit the writer
committed to — the same argument a transparency log makes when it proves the old
state is a prefix of the new one instead of asking to be trusted (RFC 6962
§2.1.2).

## Cost

One extra pass over the day, and only after the whole-file digest already
missed. Measured on this vault's 231 KB day: under 20 ms. The scan is bounded by
`MAX_EVIDENCE_SLICE_CANDIDATES`.

## Evidence

- `scripts/evidence_resolver.py` — `compile_part_slice`, `_slice_from`,
  `_daily_part_bounds`.
- `tests/test_evidence_resolver.py::test_evidence_from_one_compile_part_resolves_after_the_day_grows`
  — resolves after the day grows, and still fails when a cited byte changes.
- Live vault, 2026-08-24: `lint_memory --scope all` went from 31
  `invalid_evidence` findings to 0.
- Research: `docs/research/2026-08-24-evidence-must-name-the-slice-it-was-written-from.md`.

## Related

- [[knowledge/notes/daily-entry-quote-anchor-decision]] — what delimits the
  entry this slice is aligned to.
- [[knowledge/notes/oversized-daily-compile-decision]] — why a long day is
  compiled in parts at all.
- [[knowledge/notes/citation-relevance-gate-decision]] — the other half of a
  citation: it must also share content with the claim.
