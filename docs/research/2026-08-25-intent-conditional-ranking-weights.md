# Should the ranking prior depend on what the question asks for?

Date: 2026-08-25. Question behind it: this vault multiplies every candidate's
fused score by a trust weight — who said it (`source_authority`) and what the
page is (`type`). The weight is unconditional. The open item from 2026-08-24
says it should not be: "сначала собранные страницы" is the right rule for a
question about knowledge and the wrong rule for a question about code.

## What current practice does

**Intent-aware ranking is a query-time multiplier, not an index-time one.**
The shape the literature uses is `score = base_similarity × intent_conditioned
boost`, with the intent computed per query and the boost selected from it
([intent-aware IR overview](https://www.emergentmind.com/topics/intent-aware-information-retrieval)).
Production engines expose exactly this seam: a Vespa *rank profile* is chosen
per query and decides the ordering at query time, and 2025 added chunk-level
scoring and a global phase where the final order is decided once, with the
relevance score visible there
([Vespa ranking](https://docs.vespa.ai/en/basics/ranking.html),
[Vespa 2025 review](https://vespa.ai/resource/vespa-now-year-in-review/)).
This matches the rule this repository already wrote down when it added the type
factor: apply it once per candidate at query time, because an index-time boost
multiplies per matching term.

**Routing by query type is where the field is going — and its failure mode is
named.** Adaptive-RAG work frames per-query selection as the central problem
([RAGRouter-Bench](https://arxiv.org/pdf/2602.00296),
[SPI](https://arxiv.org/pdf/2511.16681)). The production post-mortem is the
useful half: a pre-retrieval routing mistake is not softened downstream, it
cascades — the wrong branch never recovers
([The Coverage Illusion](https://arxiv.org/pdf/2605.27220)).

## What that implies here

1. Condition at the one place the order is already decided (`_weigh_by_trust`
   inside `fuse_rrf`), not in the corpus. A corpus-side change would also
   invalidate every published generation (`NEW-81`), and a query-time change
   needs no rebuild, so before/after numbers are comparable on one generation.
2. Make the conditioned branch the *positive* detection, and leave the default
   as today's behaviour. Because a routing error cascades, the query that is
   not confidently code-shaped must keep ranking exactly as it does now.
3. One table stays one table: the conditioning decides *whether the curated
   prior applies*, it does not introduce a second set of numbers to maintain.

## What this research does not settle

Whether the conditioning buys anything measurable. That is not a literature
question — the vault has two stands (`benchmark/run_vault_retrieval.py`,
`benchmark/run_vault_application.py`) and the answer is whatever they say.
