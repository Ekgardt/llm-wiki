---
type: decision
status: active
confidence: high
source_authority: user
date: 2026-09-05
---

# An Index May Lag, And So It Can Exist At All

One-sentence summary: a generation is published from the snapshot it captured
without requiring the vault to be unchanged, and the guarantee that nothing
stale is quoted moves to the moment evidence is selected.

## Context

An outside reviewer found that this vault's search had "degraded to plain text".
It had. Measured 2026-09-05: `catalog_state.active_generation_id` was NULL, the
last activation was dated **2026-08-30**, and every retrieved row carried
`fallback_reason: "generation_unavailable"` with no vector and no graph leg. The
vectors on disk were complete — 3699 of 3699 — and unreachable, because nothing
was active.

Three causes, found in this order:

1. A span was cut at `MAX_SPAN_BYTES`, a **byte** count, which on text that is
   not ASCII lands inside a character about three times in four. Strict decoding
   raised and aborted the build. Dates from 2026-09-02.
2. A capture that lost its stability race gave up instead of taking another
   pass. A pass costs 3.6 seconds and never fails on a quiet vault; a session
   appends to the daily log on every tool call.
3. `validate_live_snapshot` re-collected the whole corpus at publication and
   demanded it be byte-identical to the snapshot taken minutes earlier.

The first two were defects and were fixed. The third is a contract, and this
page is the decision to change it.

## Decision

**Publication no longer requires the vault to be unchanged.** What is verified
is that the snapshot describes itself: its sources still hash to the manifest
recorded in `corpus_sha256`. That is tamper-evidence, it costs no I/O, and it
removes a second full collection from every publication.

The collection fence is untouched. A snapshot still has to describe a moment the
vault really passed through, proved by reading every source and reading it again
— now retried up to four times rather than surrendering on the first lost race.

**The guarantee moves to selection time.** The sources about to be quoted are
re-read before their text reaches generation, and one that no longer hashes as
captured is dropped with its spans. That is a handful of files per query rather
than the whole corpus, which is what makes it affordable.

## What was given up, stated plainly

**Completeness.** A document written after the snapshot is absent from it until
the next build, and no check at publication could find it — there is nothing yet
to check. Verifying a citation protects the truthfulness of what was found, not
the presence of what was not. This is the price every near-real-time index pays,
Elasticsearch included, and after this change it is the only price we pay:
nothing stale reaches the model, and nothing unpublished can be quoted.

## What already held and did not need changing

Activation is a compare-and-swap on a single pointer with `expected_active`, so
a builder that started from an older parent finds the pointer moved and declines
rather than overwriting a newer generation. `doctor` reports generation
freshness and names the repair. Changes after a snapshot are picked up by the
next build.

## Consequences, measured

A generation activated on the live vault for the first time since 2026-08-30. A
query now reports `effective_mode: "HYBRID"` and
`signals_used: ["lexical", "dense", "reranker"]` with no fallback reason. Full
suite 7977 passed, 256 skipped.

## Source / Evidence

- `docs/research/2026-09-05-an-index-that-went-missing-without-saying-so.md`
- `scripts/corpus_snapshot.py` — `validate_live_snapshot`, `_character_boundary`,
  `_captured_after_retries`
- `scripts/query_memory.py` — `_FreshSources`, `_authoritative_evidence`
- `tests/test_a_lagging_index_never_quotes_stale_text.py`,
  `tests/test_a_span_is_cut_between_characters.py`

## Links

- [[knowledge/notes/derived-evidence-generation-decision]] — the generation
  contract this narrows.
- [[knowledge/notes/a-question-is-not-a-conjunction-decision]]
- [[knowledge/notes/the-model-names-the-evidence-we-locate-it-decision]]
