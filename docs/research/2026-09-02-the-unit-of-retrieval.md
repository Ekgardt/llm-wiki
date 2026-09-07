# The unit of retrieval

Dated 2026-09-02. Written before changing how the corpus is cut, and it changes
what I had planned to do.

## The measurement this answers

From the compiler's own trace on this vault: of twelve retrieved candidates a
median of **two** survive the token budget and reach the answer model. The
compiler places everything it is given — zero missed. The labelled session is
ranked first for most questions. So retrieval is not the problem and the packer
is not the problem; the **unit** is.

Our unit is a heading-delimited span of a Markdown page. For a captured session
that is the whole session: about 10 KB, roughly 2 500 tokens, against a 28 672
byte answer budget. Two fit.

By judge accuracy over three runs of 200, the categories that need more than one
session are where the score collapses: multi-session **0.0216 ±0.0009** — one
row in forty-five — and temporal reasoning **0.0988 ±0.0416**.

## What I was going to do, and why the sources say not to

`docs/BACKLOG-2026-09-05.md` item Q1 says: split captured sessions at their
timestamped entries — that is, at every turn. The current work says that is the
wrong end of the trade.

> Turn-level memory is too fine-grained, leading to fragmentary and incomplete
> context, while session-level memory is too coarse-grained, containing too much
> irrelevant information. Segment-level memory can better capture topically
> coherent units.

And, directly against the naive version of my plan: *session-level retrieval
outperforms turn-level due to richer contexts*, with the best configuration
selecting granularity per instance.

So the backlog item was aimed correctly at the unit and wrongly at the
destination. Going from one 2 500-token block to fifty one-line turns trades one
failure for another.

## The band the field actually recommends

Two numbers agree from different directions:

- For analytical and multi-hop queries, 2026 benchmarking puts the useful range
  at **512 to 1 024 tokens** per chunk.
- Accuracy rises as chunk size grows from 50 to 500 and falls again by 1 000
  when "excessive content dilutes relevance"; the peak sits inside the same
  band.

Our 2 500 tokens is above it by a factor of two and a half. That is the finding:
we are not merely coarser than the reference designs, we are outside the range
anyone reports good results in.

The companion pattern is parent-document retrieval: index the small chunk for
precision, keep the larger parent available so generation does not lose the
surrounding context. Our chunks already carry `parent_page`, so half of that is
built.

## The decision

Cut a span that exceeds a bound into pieces at paragraph boundaries, never
inside a line, each keeping the heading ancestry and parent page of the span it
came from. The bound is **4 096 bytes** — about 1 024 tokens, the top of the
recommended band, chosen at the top rather than the middle because the pieces
are conversational text where a topic runs longer than in prose.

Not turns. Not sentences. A paragraph boundary in a rendered session transcript
falls between speaker turns and between the topics inside a long one, which is
the closest thing to a topically coherent unit that costs no model call.

Two consequences worth naming before they are measured:

- Seven pieces of that size fit the answer budget where two of the old ones did.
  The coverage-first shedding rule added on 2026-08-30 — drop a repeat of a page
  before the only piece of another — finally has something to choose between.
- Every chunk id changes, so the derived generation must be rebuilt. That is
  what the contract already says derived state is for: `cache/` is disposable
  and regenerable. Markdown, Git and journals are untouched.

## The cost of being wrong

**Too fine** is the failure the sources name: fragmentary context, and
single-session categories are exactly where we are strongest today because a
whole session arrives as one unit. Those categories are the guard, and the rule
written on 2026-08-31 blocks the change if any of them drops by more than the
baseline arm's own spread.

**Too coarse** is measured, and is the present state.

## What this does not settle

Per-instance granularity — the configuration the sources report as best — needs
a decision at query time about which unit to retrieve at, and that is a larger
change than one bound in the chunker. If the fixed bound helps, it is the next
question, not this one.

Whether 4 096 is the right bound for *this* corpus. It is the top of a band
measured on other corpora. The stand can answer it, one bound per arm, once the
mechanism exists.

## Sources

- [Reconstructing the Right Episode: Evaluating Interleaved Conversational Memory Beyond Long Context (arXiv 2608.25655)](https://arxiv.org/html/2608.25655)
- [On Memory Construction and Retrieval for Personalized Conversational Agents (arXiv 2502.05589)](https://arxiv.org/html/2502.05589v3)
- [A Simple Yet Strong Baseline for Long-Term Conversational Memory of LLM Agents (arXiv 2511.17208)](https://arxiv.org/html/2511.17208v1)
- [RAG Chunking Strategies: The 2026 Benchmark Guide — Prem AI](https://www.premai.io/blog/rag-chunking-strategies-the-2026-benchmark-guide/)
- [Chunk Size as an Experimental Variable in RAG Systems — Towards Data Science](https://towardsdatascience.com/chunk-size-as-an-experimental-variable-in-rag-systems/)
- [Chunking Strategies for RAG: Best Practices — Unstructured](https://unstructured.io/blog/chunking-for-rag-best-practices)
- [Best Chunking Strategies for RAG Pipelines — Redis](https://redis.io/blog/chunking-strategy-rag-pipelines/)
- Measurement: `benchmark/longmemeval-baseline-n200-r{1,2,3}-judged.json`
- Rule: `docs/research/2026-08-31-a-decision-rule-stated-before-the-run.md`
