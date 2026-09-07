---
type: decision
status: active
confidence: high
source_authority: ai-derived
date: 2026-09-03
---

# The Model Names The Evidence, We Locate It

One-sentence summary: a grounded reply supplies the citation identifier and
nothing else; the path, revision, byte range and hashes come from the manifest
this process built, and a locator field written into the reply is not read.

## Context

The evidence manifest handed to generation carries nine fields per span:
`citation_id`, `relative_path`, `source_sha256`, `revision`, `byte_start`,
`byte_end`, `line_start`, `line_end`, `span_sha256`. The verifier required the
reply to reproduce all nine byte for byte, and refused the citation otherwise
with "citation does not match supplied evidence".

Measured over 200 questions on 2026-09-02, that was the single largest failure
on the stand: **18 answers destroyed**, in every case after the model had found
the right span and mistyped a hash or an offset while transcribing it.

## Decision

The reply is trusted for the identifier. Everything else is taken from the
manifest we built and handed to generation, and what the reply says about those
fields is not read at all.

This is **stronger** than comparing the two, not weaker. A citation can no
longer be believed on the model's word, because the model's word about a
locator is never consulted. A reply that writes `../outside.md` into a path, or
a wrong hash into a source, changes neither what is verified nor what is
published.

An identifier naming evidence that was never supplied is still dropped, and
every claim resting on it is dropped with it.

## What did not move

`verify_evidence_span` binds every published citation to the vault: the path
resolves inside it, the file still hashes to what generation was shown, and the
recorded byte range still holds the recorded span. A span that fails is dropped.
Entailment is still not verified and is still not claimed.

## What the field says

The current guidance for grounded generation is that the model emits the source
identifier while the system resolves and renders the locator, because mixing the
two increases formatting errors rather than catching them. The literature on
citation failure separates the two halves we had merged: failing to *locate*
evidence, and failing to *transcribe* the citation for evidence already located.
Only the first is a grounding failure. We were punishing both as one.

## Consequences, measured

On 50 questions, seed 101, immediately after the change: answers 33 → 35,
correct 27 → 28, correct per question 0.5400 → 0.5600. **That is one question
and it is inside the noise of n=50** — the class had already been reduced to
silent abstentions by the earlier claim-level change, so most of its cost was
paid before this measurement began. The argument for the change is the removed
failure mode, not that number.

## Source / Evidence

- `scripts/query_memory.py` — `_verified_citations`, `_published_citation`,
  `_span_still_holds`
- `scripts/evidence_resolver.py` — `verify_evidence_span`
- `tests/test_grounded_qa.py` —
  `test_a_tampered_locator_cannot_be_believed_because_it_is_not_read`
- Runs: `benchmark/longmemeval-claimlevel-n200-r1.json`

## Links

- [[knowledge/notes/a-failing-claim-does-not-destroy-the-answer-decision]] —
  the blast-radius half of the same problem.
- [[knowledge/notes/citation-relevance-gate-decision]]
- [[knowledge/notes/part-scoped-evidence-decision]]
- [[knowledge/notes/an-index-may-lag-decision]] — links to this page.
