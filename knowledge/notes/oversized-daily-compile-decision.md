---
type: decision
status: proposed
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

Proposed, not implemented. It changes the compile input model, which the
transactional compile tests pin, so it needs the owner's sign-off first. Until
then the pass still refuses, and now records which file did not fit.

## Source

- `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`, entry `NEW-38`
- https://futureagi.com/blog/rag-summarization/
- https://www.f22labs.com/blogs/map-reduce-for-large-document-summarization-with-llms/
- https://galileo.ai/blog/llm-summarization-strategies
- https://www.oreilly.com/radar/why-multi-agent-systems-need-memory-engineering/
- https://machinelearningmastery.com/5-architectural-patterns-for-persistent-memory-and-state-in-ai-agents/

## Links

- [[knowledge/notes/reliable-memory-stage-2]]
