---
type: decision
status: accepted
confidence: medium
source_authority: web
---

# Oversized Daily Logs Are Split, Not Skipped and Not Fatal

One-sentence summary: a daily log larger than the compile input budget should be
split at entry boundaries and compiled as parts under one atomic per-source
receipt, rather than skipped or allowed to fail the whole pass.

## The problem, measured

On the installed vault this session's own daily log reached 70 KB, above the
compile input budget. `_compile_batches` raises
`daily source exceeds compile input budget`, which fails the entire pass — so
the other three daily logs, all well inside the budget, were never compiled
either. A single long session makes the vault permanently uncompilable.

Source: `scripts/compile_memory.py`, the singleton check in `_compile_batches`;
observed on `knowledge/daily/2026-08-21.md`, 70063 bytes.

## What current practice says

Two findings, from a review of 2026 sources on long-input LLM pipelines and on
agent-memory durability.

The canonical answer to an input larger than the window is neither refusal nor
omission. It is map-reduce or hierarchical summarisation: split into chunks,
process each, then reduce the partial results. Hierarchical merging matches or
slightly beats full-context processing at lower cost. Production guidance is to
keep a source reference — page number or chunk id — with every intermediate
output so each one stays verifiable against the original.

The second finding constrains how: partial writes are what corrupt a shared
memory. Atomic updates, and resumable checkpoints, are what keep it consistent.

Together they rule out both options in the original question. Skipping loses
knowledge silently, which the first finding rejects. Failing the whole pass
punishes every other source for one, which nothing supports.

## The decision

An oversized daily is split at entry boundaries into budget-sized parts. Each
part carries the daily's logical path plus its byte range, so a citation still
resolves to a real span of a real file. Parts are compiled as the existing
batches already are, and the daily's compile receipt is written only when every
one of its parts has succeeded — the source is compiled entirely or not at all.

Not decided here: the chunk overlap, and whether the reduce step needs its own
pass for a daily that produces many parts. Both are answerable once the split
exists and can be measured.

## Status

Accepted 2026-08-21 and implemented.

The split is by bytes, not tokens, and that is deliberate: the same file has to
split the same way every time, because that is what lets an interrupted run
resume from the parts it already committed. The token budget still decides how
many parts travel together in one batch, which is the packing that already
existed. A part that still will not fit the budget refuses as before, and names
the file.

Resumability falls out of the existing receipts rather than a new marker. A
part is a source in its own right, so it gets its own receipt keyed by its own
digest; a day counts as compiled when every one of its parts does. A run that
dies halfway leaves receipts for what committed, and the next run is offered
only the rest.

Verified on the installed vault: the 70063-byte daily splits into five parts,
contiguous and each inside the bound, and the compile pass no longer fails on
it.

## Source

- `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`, entry `NEW-38`
- https://futureagi.com/blog/rag-summarization/
- https://www.f22labs.com/blogs/map-reduce-for-large-document-summarization-with-llms/
- https://galileo.ai/blog/llm-summarization-strategies
- https://www.oreilly.com/radar/why-multi-agent-systems-need-memory-engineering/
- https://machinelearningmastery.com/5-architectural-patterns-for-persistent-memory-and-state-in-ai-agents/

## Links

- [[knowledge/notes/reliable-memory-stage-2]]
- [[knowledge/notes/daily-entry-boundary-decision]] — what delimits one entry in the log this decision cuts.
- [[knowledge/notes/part-scoped-evidence-decision]] — how a reader verifies a page written from one of these parts.
- [[knowledge/notes/daily-entry-quote-anchor-decision]] — links to this page.

## Related
- [[knowledge/notes/session-promotion-policy-decision]] — хранение безусловно; повышение до страницы решает консолидация по всей записи.
- [[knowledge/notes/bounded-capture-excerpt-decision]] — a transcript larger than
  the evidence bound is excerpted at both ends rather than refused.
