# Is there anything better? A second pass over agent memory, 2026-08-23

## Why a second note

The first pass answered one question — keep the session or keep a distillation of
it — with one controlled ablation. The owner asked whether that is really the best
available, so this pass surveys the whole live landscape: what each family of
memory architecture claims, what independent evidence supports, what it costs,
and which parts this vault already has.

## The families, and what the evidence actually shows

**1. Retrieval store over the material (RAG, "Pattern B").** The 2026 survey of
autonomous-agent memory calls this the most production-ready family and
recommends starting here, graduating to tiered memory with learned control "only
if empirical gains justify the complexity". Independent multi-dataset work
(MemoryBench: 11 datasets, 3 domains, 2 languages) found A-Mem, Mem0 and MemoryOS
**cannot consistently outperform a naive RAG baseline** over the same material;
their edge on LoCoMo does not generalise.

**2. Extraction into artifacts.** The controlled ablation from the first note:
verbatim chunks beat extracted artifacts by 15.9 points (LoCoMo) and 22.0
(LongMemEval-S); a union store of both matched chunks alone. Extraction commits
to relevance before the question exists.

**3. Hierarchical virtual memory (MemGPT / Letta).** Main context plus recall and
archival stores, with the agent editing its own memory blocks. The survey says it
works best for extended multi-session agents. It is a genuine architecture, not a
benchmark artefact — but it is also a runtime, not a file format, and adopting it
means adopting its process model.

**4. Reflective memory (Reflexion and descendants).** Post-mortem self-critique
stored and re-read. The strongest documented gain for **coding** agents in the
survey: 91% pass@1 on HumanEval against an 80% baseline. The survey also warns
about "a lesson learned in one context applied blindly in another" and requires
confidence scoring and expiry.

**5. Procedural libraries (Voyager-style verified skills).** 15.3x faster
progression in its domain. The survey's context table is explicit: personal
assistants need semantic memory, **coding agents need procedural memory —
verified patterns**, games need episodic plus procedural.

**6. Temporal knowledge graphs (Zep / Graphiti).** The right answer when facts
change under you. Its headline number is also the one that moved most under
scrutiny: 84% to 75.14% to 58.44%.

**7. Long context instead of memory.** Models that score near-perfect on LoCoMo
drop to 40-60% on MemoryArena, where memory has to be *used* to act rather than
merely recalled. The survey's efficiency finding is blunt: "a handful of highly
relevant passages into moderate-length context often beats both pure long-context
and pure retrieval".

**8. Sleep-time compute / offline consolidation.** Idle-time passes that
reorganise memory rather than answer questions. Letta reports an 18% accuracy gain
and 2.5x lower cost per query, because the work moves off the user's latency path;
sleep-consolidation research (SCM) reports gains that grow with the number of
offline passes. The survey lists principled consolidation as open frontier number
one, and describes the mechanism this vault should recognise: raw episodic records
in a **hot probation buffer**, promoted to long-term storage only after validation
— "hippocampal-to-neocortical transfer".

## Efficiency, measured

- Two-stage hybrid retrieval with neural reranking is the best-measured
  configuration: Recall@5 0.816, MRR@3 0.605, against 0.433 MRR@3 without the
  reranker. Reciprocal-rank fusion beats either leg alone (NDCG 0.7068 vs 0.6983
  BM25, 0.6953 dense).
- Contextual enrichment of chunks before embedding adds +2.8pp Recall@5 dense,
  +2.2pp hybrid.
- Retrieval overhead is 200-500 ms per pipeline call; storage capacity is a
  non-issue for vector stores — "the bottleneck is retrieval quality, not
  capacity".
- Write-side cost of keeping the material is near zero: in the ablation,
  extraction was $0.14 of a $2.92 pipeline; the answering call dominates.

## What this vault already has

Measured against that list, the retrieval side is already at the recommended
configuration: SQLite FTS5 plus dense vectors with reciprocal-rank fusion,
contextual enrichment before indexing (`contextual_retrieval.py`), a cross-encoder
reranker (`reranker.py`), a structural evidence graph, typed provenance weighting
from one table, page-level diversity in fusion, temporal claims, weekly
consolidation (`reflection.py`, `build_tiers.py`, `archive_stale.py`), a skills
and playbook layer, and a nightly idle window that is exactly the slot sleep-time
compute asks for.

The gap is not the retriever. It is that **the material never arrives**: a
classifier decides at write time whether a session is worth keeping at all, and on
this vault's own sessions it keeps one in forty.

## Conclusion

1. Nothing in the landscape is demonstrably better than "keep the material and
   retrieve over it" for this vault's shape — and the systems that claim to be
   cannot show it outside their own harness.
2. The proposal from the first note is also what the survey independently
   recommends: a hot buffer of raw episodes, validated promotion into durable
   pages, retrieval over the union.
3. Two further upgrades are supported by evidence, cheap here because the
   machinery exists, and worth doing **after** the raw layer, in this order:
   - **Consolidation in the idle window** over the new raw layer — the vault
     already runs nightly and weekly passes; this is where Letta's 18% and 2.5x
     came from, and it costs no query-time latency.
   - **Post-mortem lessons into the procedural layer** — the survey's single
     largest documented gain for coding agents, and the vault already has skills,
     playbook crystallisation and a reflection pass to attach it to.
4. What not to adopt: a branded memory OS whose advantage does not reproduce, a
   temporal graph in place of what we have, or long context in place of
   retrieval.
5. The gate stays as the critics recommend: measure on this vault's own data, and
   require a clear margin over the baseline before taking on more machinery.

## Sources

- "Memory for Autonomous LLM Agents: Mechanisms, Evaluation, and Emerging
  Frontiers" (survey, 2026) — https://arxiv.org/html/2603.07670v1
- "From Storage to Experience: A Survey on the Evolution of LLM Agent Memory
  Mechanisms" — https://openreview.net/forum?id=l9Ly41xxPb
- "MemoryBench: A Benchmark for Memory and Continual Learning in LLM Systems" —
  https://arxiv.org/abs/2510.17281
- "Verbatim Chunks Beat Extracted Artifacts" — https://arxiv.org/html/2601.00821v3
- "The Benchmark Theatre" — https://essays.bloo-mind.ai/posts/2026-05-20-mem-eval/
- "Are We Ready For An Agent-Native Memory System?" — https://arxiv.org/pdf/2606.24775
- "SCM: Sleep-Consolidated Memory with Algorithmic Forgetting" —
  https://arxiv.org/abs/2604.20943
- Letta, "Sleep-time compute" — https://www.letta.com/blog/sleep-time-compute/
- "Hybrid Search: BM25, Vector & Reranking Reference 2026" —
  https://www.digitalapplied.com/blog/hybrid-search-bm25-vector-reranking-reference-2026
- "From BM25 to Corrective RAG: Benchmarking Retrieval Strategies" —
  https://arxiv.org/html/2604.01733v1
