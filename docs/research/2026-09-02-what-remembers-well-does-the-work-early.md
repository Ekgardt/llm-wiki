# What remembers well does the work early

Dated 2026-09-02. Written because incremental retrieval fixes are worth tenths
and the owner asked for a multiple — and asked to look outside human memory.

## Three organisms, one principle

**Corvids.** A scrub jay recovers perishable and non-perishable caches
correctly, which requires integrating *a semantic rule* — how long each food
stays fresh — with *an episodic record* of which cache went where on which day.
Clark's nutcrackers and pinyon jays, which depend on caches to survive winter,
recover more accurately than scrub jays after a week. The bird is not searching.
It stored what, where and when **at caching time**, and recall is a lookup
against that record plus a rule.

**Bees and ants.** A desert ant counts its steps and tracks its turns,
integrating them into a single continuously updated vector that points home.
Bees hold several such vectors in parallel long-term memories and recall one at
a familiar location. They do not store the journey at all — they store the
*computed answer*, and they reach vertebrate-level navigation with a tiny
fraction of the neurons.

**Slime mould.** *Physarum polycephalum* has no neurons. It lays down slime as
an external mark so it does not revisit ground it has already covered — an
externalised spatial memory, proposed as the functional precursor of internal
memory.

The shared principle is not a better index and not a cleverer search. It is
this: **the work happens when the memory is written, and what is stored is the
thing that will be needed — a fact with its time, a computed vector, a mark on
the ground.** Retrieval is then cheap because most of the thinking already
happened.

## What we do instead

We store the conversation verbatim and do every bit of the work at read time:
three retrieval legs, fusion, reranking, budget packing, and an answer model
that must reconstruct a fact from raw text. Nightly compile writes pages, but
nothing extracts *facts with time and subject* that retrieval could look up
directly.

That is the asymmetry. A multi-session question is a join — this person's X, and
separately their Y — and we ask a text search to perform a join. A temporal
question needs a date to bound a search, and we ask a text search to compare
dates it has not extracted.

## What the field measures, including where it disagrees

The systems above us do the work early:

- Canonical facts as stable write units, with a scoped temporal index allowing
  localized updates rather than global maintenance. **MemForest: 70.4% with a 4B
  answer model, 79.8% with 30B.**
- Schema-guided facts consolidated at write time, plus facts formed on demand and
  promoted when several sources support them. **MemSIF beats the strongest
  baseline by 2.29–8.79% on LongMemEval-S.**

And the honest counter-evidence, which I have cited before and will not bury:
*Storage Is Not Memory* argues extraction at ingestion is the **wrong** primitive
because content discarded before the query is known cannot be recovered — and
its verbatim-first design reaches 87.8 on LongMemEval, above both systems above.

So the field genuinely disagrees about whether to extract early. What both camps
share is that neither throws the raw record away. The reconciliation is not a
choice between them: keep the verbatim episode as authority, and build the
fact index as **derived** state beside it.

That is already this vault's contract. Markdown, Git and journals are
authoritative; graph, FTS, vector and tier state are disposable and derived.
A derived fact index breaks nothing, and the approved superset target already
names temporal claims and episodic/semantic memory as part of the product.

## The proposal

Extract canonical facts at write time — subject, relation, value, valid time,
and the span they came from — into a derived generation beside the verbatim
session. Add a retrieval leg that looks facts up by subject and time instead of
by similarity.

Why this and not another retrieval tweak: a multi-session question becomes a
join over two facts instead of a text search hoping to surface both sessions,
and a temporal question becomes a comparison of two dates instead of a hope that
the model derives them. Those two categories are 99 of our 200 questions and
they sit at 0.19 and 0.18.

## What I am not claiming

That this reaches 70–80% here. Those numbers come from other pipelines with
other answer models, and our answer path additionally refuses most questions on
citation grounds — a separate decision that is still open. What is defensible:
this is the only change on the table whose reference implementations sit in that
range, and the biological evidence and the benchmark evidence point the same way.

It is a large change and needs a decision page and the owner's yes before code.

## Sources

- [Episodic-like memory during cache recovery by scrub jays — Nature](https://www.nature.com/articles/26216)
- [Revisiting episodic-like memory in scrub jays — PMC](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11880068/)
- [Do Clark's nutcrackers demonstrate what-where-when memory? — Animal Cognition](https://link.springer.com/article/10.1007/s10071-011-0429-y)
- [Parallel vector memories in the brain of a bee — PNAS](https://www.pnas.org/doi/10.1073/pnas.2402509121)
- [Route retracing: way pointing and multiple vector memories in trail-following ants — PMC](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC10906666/)
- [Slime mold uses an externalized spatial "memory" to navigate — PNAS](https://www.pnas.org/doi/10.1073/pnas.1215037109)
- [MemForest: Hierarchical Temporal Indexing (arXiv 2605.23986)](https://arxiv.org/html/2605.23986)
- [MemSIF: From Structured Interactions to Dual-Track Fact Memory (arXiv 2608.01742)](https://arxiv.org/html/2608.01742)
- [MemReader: From Passive to Active Extraction (arXiv 2604.07877)](https://arxiv.org/pdf/2604.07877)
- [Storage Is Not Memory: A Retrieval-Centered Architecture (arXiv 2605.04897)](https://arxiv.org/html/2605.04897v1) — the counter-argument
- Measurement: `benchmark/longmemeval-unit-n200-r1-judged.json`
