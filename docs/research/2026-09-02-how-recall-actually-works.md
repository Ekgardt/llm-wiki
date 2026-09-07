# How recall actually works, and which half of it we should copy

Dated 2026-09-02. Written because the owner asked whether searching by words is
the right shape at all, and pointed at human memory as the comparison.

## First, the premise, corrected

We do not search by words alone. A query runs three legs — lexical FTS5, dense
embeddings, and the code/knowledge graph — fused by reciprocal rank, weighted by
typed provenance. The word-matching that is visible in the citation gates sits
at the *end*, on the answer, not at the front on the search.

So the inefficiency the question points at is real but it is not "we match
strings". It is something else, and the brain names it precisely.

## What the brain does that we do not

**Retrieval is cue-based pattern completion, and it iterates.** The CA3
subregion's dense recurrent connections allow auto-associative reinstatement of
a whole prior pattern from a partial cue after a single exposure. The
chronometry is measurable: a sensory cue reaches the medial temporal lobe inside
500 ms, and hippocampal pattern completion with cortical reinstatement runs
between 500 and 1500 ms. What comes back is not the cue's nearest neighbours —
it is a reconstructed episode, and that reconstruction then acts as the cue for
the next one.

Our retrieval is one shot. Query in, ranked spans out, done. Nothing that comes
back is ever used to ask again.

That is exactly the shape of the questions we fail. A multi-session question
needs a fact from session A to know that session B is relevant at all; a
temporal one needs a date found in one place to bound a search in another.
Measured today: multi-session 0.19 and temporal 0.18 after the chunking fix,
against 0.42–0.52 on the single-session categories. One-shot retrieval cannot
close that, however good the index — the second cue never exists.

**Consolidation, we already have.** Nightly compile turns captured days into
durable pages, supersession updates them instead of overwriting, and the graph
holds the associations. That is systems consolidation in the ordinary sense, and
it is the part of the architecture the analogy supports rather than accuses.

## What the brain does that we must not copy

Human memory is reconstructive, and fuzzy-trace theory separates the two traces
it keeps: verbatim traces reinstate the contextual surface of an event, gist
traces keep only its semantic content — and gist is what survives. The
well-documented consequence is that people confidently recall the meaning of
something and invent its details.

That is the opposite of what this vault promises. Our citation gates exist
precisely to refuse the confabulation the brain performs by design. So the
answer to "how does the brain work" is: on the *finding* side we should copy it,
on the *answering* side copying it would be the bug we are guarding against.

## What this implies

The next architectural item is not a better index. It is **iterative retrieval**:
use what the first pass returned as the cue for a second pass, bounded by a hop
count and the existing optional-stage budget. It is the mechanism the brain uses,
it is what the failing categories need, and it does not touch the answer
contract at all.

That is a design change and needs its own decision before any code.

## Sources

- [Holistic Recollection via Pattern Completion Involves Hippocampal Subfield CA3 — J. Neurosci.](https://www.jneurosci.org/content/39/41/8100)
- [Hippocampal pattern completion is linked to gamma power increases — eLife](https://elifesciences.org/articles/17397)
- [A Neural Chronometry of Memory Recall — Trends in Cognitive Sciences](https://www.cell.com/trends/cognitive-sciences/fulltext/S1364-6613(19)30235-9)
- [An Enduring Role for Hippocampal Pattern Completion after a 24 h Delay — J. Neurosci.](https://www.jneurosci.org/content/44/18/e1740232024)
- [Fuzzy-trace theory — verbatim and gist traces](https://en.wikipedia.org/wiki/Fuzzy-trace_theory)
- [The Verbatim Effect: People Remember Gist Better Than Details](https://effectiviology.com/verbatim-effect/)
- [Agent Memory Architectures: 5 Patterns and Trade-offs — Atlan](https://atlan.com/know/agent-memory-architectures/)
- [State of AI Agent Memory 2026 — Mem0](https://mem0.ai/blog/state-of-ai-agent-memory-2026)
- Measurement: `benchmark/longmemeval-unit-n200-r1-judged.json`
