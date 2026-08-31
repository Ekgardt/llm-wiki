# Where we stand against the field, 2026-08-29

Dated research behind the backlog. Every number is either measured here or
cited; nothing is estimated.

## The headline, stated against ourselves

| | ours, measured | field, 2026-08-29 |
|---|---|---|
| LongMemEval accuracy | **0.320** deterministic, 0.360 judged, n=50 | **94.4** |
| tokens per query | median **4 302**, max 5 976 | ~6 900 |
| LoCoMo | not run | 92.5 |
| BEAM (1M / 10M tokens) | not run | the production-scale benchmark |

We spend fewer tokens than the state of the art and answer a third as often as
it does. **That is not efficiency — it is abstention.** Of 50 questions, 26
returned `insufficient_evidence`; accuracy when the system does answer is
14 of 18 = **0.78**. The binding constraint is refusal, not error.

Two categories carry the loss: `multi-session` **0.083** and
`temporal-reasoning` **0.154**. Those are exactly the two where the field
reports its largest recent gains — **+29.6 points on temporal reasoning** and
**+23.1 on multi-hop**.

## What the field does that we do not

**Bitemporal facts.** Current temporal memory stores facts as
`(subject, relation, object, t_start, t_end)` and separates *valid time* — when
the fact held in the world — from *transaction time* — when the system came to
believe it. That supports as-of retrieval: what was true when it mattered,
rather than a mixture of stale and current. Engram writes lossless episodes on
a fast path and distils them asynchronously into such a graph; TOKI adds an
operator algebra for resolving contradictions between them. A graph-native
bitemporal store reports about 90% on direct user-statement recall and 80% on
knowledge updates — and is *still* weak on combining across sessions, which
says the multi-session gap is not solved by bitemporality alone.

**Calibrated abstention as a trained objective.** Over-refusal is a named,
measured failure with its own line of work: Abstain-R1 trains abstention and
post-refusal clarification with verifiable rewards; TruthRL does the same for
queries clear in meaning but unresolvable from the given information;
AgentAbstain evaluates whether a tool-using agent knows when not to act. The
field's framing is a *calibration* problem with two error directions, not a
safety switch. Ours is set to one direction and never measured against the
other.

**Two-stage retrieval where the reranker carries the precision.** The
production pattern is hybrid retrieval followed by a cross-encoder over the top
candidates, and the reranker is where most of the precision gain comes from.
RRF fusion alone reaches about 91% recall@10. Late interaction (ColBERT) is
recommended specifically for domain corpora where exact phrasing matters —
legal, medical, **code** — after RRF has been shown insufficient, not before.

**Token discipline as engineering, not as taste.** A compact tool-schema format
gives an average **40% reduction in tokens per tool**. Context compaction took
a five-ticket pipeline from 208K to 86K tokens, **58.6%**. Anthropic's own
evaluations report context editing worth **+29%** and, combined with a memory
tool, **+39%**.

**The competitor's own published claim.** A 2026 study of codebase-memory
reports a tree-sitter knowledge graph cutting agent token use by roughly **10×**
and tool calls by **2.1×** across 31 repositories. That is the bar the
efficiency goal is really measured against, and our current stand ratio is
3.82× *behind* on tokens.

## What we already have that the field treats as hard

Not everything is a deficit, and the backlog should not spend effort twice.

- **Provenance and trust weighting.** `source_authority` with a stated
  hierarchy, applied as one weight across every retrieval path. The 2026
  reliability work argues memory quality depends on source trust and write-path
  legitimacy rather than recall — we have the mechanism it asks for.
- **Recoverable transactions with before/after images.** This vault recovered
  1 000 lost journal events from them today. The field's memory systems mostly
  do not have this.
- **Stability.** Five parity runs, identical grades every time, against three
  grade changes on the competitor's side in the same five runs.

## The open problems the field names

Cross-session identity, temporal abstraction at scale, and memory staleness.
All three are ours too, and the third is the one we already have a mechanism
for and do not use.

## Sources

- [State of AI Agent Memory 2026 — mem0](https://mem0.ai/blog/state-of-ai-agent-memory-2026)
- [AI Memory Benchmarks 2026: LoCoMo, LongMemEval & BEAM — mem0](https://mem0.ai/blog/ai-memory-benchmarks-in-2026)
- [A Graph-Native Bitemporal Memory Store for Conversational AI Agents (arXiv 2607.26520)](https://arxiv.org/html/2607.26520v1)
- [TOKI: A Bitemporal Operator Algebra for Contradiction Resolution in LLM-Agent Persistent Memory (arXiv 2606.06240)](https://arxiv.org/pdf/2606.06240)
- [MemoTime: Memory-Augmented Temporal Knowledge Graph Enhanced LLM Reasoning (arXiv 2510.13614)](https://arxiv.org/pdf/2510.13614)
- [Abstain-R1: Calibrated Abstention and Post-Refusal Clarification via Verifiable RL (arXiv 2604.17073)](https://arxiv.org/html/2604.17073v1)
- [Know Your Limits: A Survey of Abstention in Large Language Models](https://www.researchgate.net/publication/393331033_Know_Your_Limits_A_Survey_of_Abstention_in_Large_Language_Models)
- [Hybrid Search and Re-ranking in Production RAG 2026 — AppScale](https://appscale.blog/en/blog/hybrid-search-and-reranking-production-rag-bm25-dense-cross-encoder-2026)
- [ColBERTv2: Effective and Efficient Retrieval via Lightweight Late Interaction (arXiv 2112.01488)](https://arxiv.org/pdf/2112.01488)
- [Context Engineering for AI Agents: Token Economics — Maxim](https://www.getmaxim.ai/articles/context-engineering-for-ai-agents-production-optimization-strategies/)
- [Code Intelligence & Code-Graph Indexing for AI Agents — Anthony West](https://anthonywest.co.uk/research/code-intelligence-indexing-2026-openai)
- [LARGER: Lexically Anchored Repository Graph Exploration and Retrieval (arXiv 2605.16352)](https://arxiv.org/pdf/2605.16352)
- [When to use Graphs in RAG (arXiv 2506.05690)](https://arxiv.org/pdf/2506.05690)
