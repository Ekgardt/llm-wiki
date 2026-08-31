# What actually closes the quality gap

Dated 2026-08-30. Written to decide what to build next, because the efficiency
work is nearly done and the quality gap is the whole remaining distance to the
goal.

## Where we stand

Measured on this vault, LongMemEval n=50, seed 13, five runs:
**0.2857, 0.3200, 0.3469, 0.3542, 0.3958.**

Published results on the same benchmark: Mem0 **94.4** at ~6 900 tokens per
query; Supermemory **95 % Recall@15** adding ~720 tokens; True Memory Pro
**87.8** (3-run mean); Memoria **88.78**; TSM **74.80**.

The distance is not a few points. It is a factor.

## The finding that reorders everything

The most directly comparable system is *Storage Is Not Memory* (arXiv
2605.04897). Its True Memory runs **as a single SQLite file on commodity CPU
with no external database, vector index, graph store, or GPU** — our exact
constraints — and reaches 87.8 on LongMemEval, 93.0 on LoCoMo and 76.6 on
BEAM-1M. So local-first, CPU-only, SQLite is not the ceiling. Our number is.

The same paper ran a **56-configuration grid**, 7 embedder classes × 8
rerankers, and found a **3.2 percentage-point spread** overall — 1.3 points
inside the best subfamily. Query expansion bought **1.0 point**. Its conclusion
is stated flatly: *component choice within the retrieval pipeline moves accuracy
by an order of magnitude less than the retrieval architecture itself.*

That rules out most of what a backlog naturally fills up with. Swapping the
embedder, tuning fusion weights, adding query expansion — together those are
worth a few points against a gap of forty.

## What the architecture difference actually is

Measured here on 2026-08-30 from the compiler's own trace: of twelve retrieved
candidates, a **median of two** survive the budget and reach the answer model.
The compiler places everything it is given — zero missed. The narrowing is the
budget, and it is the budget because of the **unit**.

Our retrieval unit is a heading-delimited span of a Markdown page
(`corpus_snapshot._chunks`). For a captured session that is the whole session,
about 10 KB against a 28 672-byte answer budget. Two fit.

True Memory's unit is a **message**. Its pipeline takes top-k FTS5 matches and
top-k dense neighbours, fuses them with RRF (`w_fts = w_vec = 1`, `k = 60`),
reranks the **top 100**, and hands the answer model the **top 10**.

Ten units spanning ten sessions, against our two units from one session. A
multi-session question needs facts from two sessions; a temporal one needs a
date from one and a fact from another. With two slots spent on one page,
neither is answerable no matter how good the ranking is. That is exactly where
our accuracy collapses — multi-session 0.18, temporal 0.15 — and exactly where
the field reports its biggest gains: **+29.6 points temporal, +23.1 multi-hop**
for Mem0's temporal work.

So the first item is not a model. It is the granularity of what we store and
retrieve.

## The second difference: the reranker is off

Ours is conditional. `reranker.should_rerank` runs only when the profile asks,
the lexical and dense ranks disagree, or the scores are close; `DEFAULT_RERANK_DEPTH`
is 20; and `configured_reranker_identity` returns nothing unless
`LLMWIKI_RERANKER_MODEL` and a pinned immutable revision are both set. In the
default installation it never runs.

The reference designs run it always, over 100 candidates, and it is where their
precision comes from. The cost is known and small: **100–300 ms on CPU to score
50 candidates** with MiniLM-L-6-v2 (22M), and cross-encoders are reported to
lift **+5 to +15 NDCG@10** on MTEB and BEIR. True Memory Pro uses
`gte-reranker-modernbert-base` (149M); its Edge tier uses
`cross-encoder/ms-marco-MiniLM-L-6-v2` (22M). The 2026 production default is
`BGE-Reranker-v2-m3`.

## The third: time is not modelled, only matched

The field's answer to temporal questions has two halves. The cheap half is a
**conditional temporal boost** — True Memory multiplies scores by 1.3 when the
query carries temporal intent. The real half is a **bitemporal store**: each
fact carries a valid-time interval (when it was true in the world) and a
transaction-time interval (when it was recorded), so "what did I believe in
June" and "what was true in June" are different queries. Graph-native
bitemporal stores and the TOKI operator algebra both take this shape, and TSM
sets its state of the art on precisely the three time-dependent categories.

Knowledge updates are the same mechanism seen from the other side: the field
keeps history additively and computes a **current-state view** rather than
deleting. That matches this vault's supersession rule already.

## What this says about measurement

Every published number above is a 3-run mean over the full 500 questions. Ours
are single runs at n=50 whose observed spread is about **0.07**. Nothing smaller
than that is detectable, which means most of the backlog below is unprovable
until the measurement is fixed. That is why measurement is item one and not item
nine.

LoCoMo and BEAM have never been run here at all, so two of the three benchmarks
the field reports on are blank for us.

## Open questions

Whether turn-level chunking hurts the categories that currently work.
Single-session recall is our strongest suit precisely because the whole session
arrives as one unit; splitting it could lose the context that makes it
answerable. The measurement must watch those categories as the guard.

What our token cost per query actually is. The comparison points are ~6 900
(Mem0) and ~720 (Supermemory) added tokens; we have never measured ours in
those terms, so "efficiency parity" is currently a claim about tool-call tokens
against cbm, not about answer-path tokens against memory systems.

## Sources

- [Storage Is Not Memory: A Retrieval-Centered Architecture for Agent Recall (arXiv 2605.04897)](https://arxiv.org/html/2605.04897v1)
- [State of AI Agent Memory 2026: Benchmarks & Trends — Mem0](https://mem0.ai/blog/state-of-ai-agent-memory-2026)
- [AI Memory Benchmarks 2026: LoCoMo, LongMemEval & BEAM — Mem0](https://mem0.ai/blog/ai-memory-benchmarks-in-2026)
- [Introducing Temporal Reasoning in Mem0](https://mem0.ai/blog/introducing-temporal-reasoning-in-mem0)
- [Research — Supermemory](https://supermemory.ai/research/)
- [A Graph-Native Bitemporal Memory Store for Conversational AI Agents (arXiv 2607.26520)](https://arxiv.org/abs/2607.26520v1)
- [TOKI: A Bitemporal Operator Algebra for Contradiction Resolution in LLM-Agent Persistent Memory (arXiv 2606.06240)](https://arxiv.org/pdf/2606.06240)
- [Beyond Dialogue Time: Temporal Semantic Memory for Personalized LLM Agents (arXiv 2601.07468)](https://arxiv.org/pdf/2601.07468)
- [LongMemEval-V2: Evaluating Long-Term Agent Memory (arXiv 2605.12493)](https://arxiv.org/html/2605.12493v1)
- [Reranking & Cross-Encoders for RAG: BGE, Cohere, Jina (2026)](https://localaimaster.com/blog/reranking-cross-encoders-guide)
- [Shallow Cross-Encoders for Low-Latency Retrieval (arXiv 2403.20222)](https://arxiv.org/pdf/2403.20222)
