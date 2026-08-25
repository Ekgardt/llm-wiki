# What a session should leave behind (2026-08-23)

## Why this was researched

Measured on this machine the same day: of 40 real sessions, the classifier kept
one. It never kept anything it should not have — there were no false promotions
in either run — but a memory system that discards 39 sessions in 40 is not
remembering. The question this note answers is what current work says the write
path of an agent memory should do: decide at write time what is worth keeping, or
keep the material and decide later.

## What the field says

**The controlled answer is: do not decide at write time.** A 2026 ablation held
the retriever, reranker, judge and backbone constant and swapped only what was
stored. Verbatim chunks of the conversation beat LLM-extracted artifacts by 15.9
points on LoCoMo (43.9% vs 28.0%) and by 22.0 points on LongMemEval-S (67.4% vs
45.4%). The extracted-artifact pipeline never beat naive RAG despite costing more
to build. The authors name the mechanism *lossy distillation*: "extraction commits
to relevance at write-time before questions exist, while verbatim storage defers
relevance decisions to query-time."

**Structure is not the enemy; substitution is.** In the same study a union store
— verbatim chunks and extracted artifacts indexed together — scored 42.5%,
statistically indistinguishable from chunks alone. Their conclusion is explicit:
"structure may coexist with verbatim text, but substituting it forfeits 15.9
points."

**Their parameters**: 512-character chunks with 100 characters of overlap aligned
to line breaks; per-turn and per-session extraction behaved alike; artifacts
carried type, a verbatim grounding quote, source role, turn index and confidence.
Cost was not the deciding factor either way — $12.50 vs $14.90 per 1,000 correct
answers, with extraction only $0.14 of a $2.92 pipeline; the answering call
dominates.

**Where extraction still wins**: a synthetic multi-hop benchmark where every
planted fact had been pre-shaped into exactly one artifact (81.0% vs 55.5%). The
advantage inverts on both real benchmarks — the premise holds "only when the
world has been arranged to satisfy it."

**The known weakness of verbatim storage** is abstention: chunks were worse at
refusing unanswerable questions (46.7% vs naive RAG's 70.0%). Their evaluation is
English chat-style QA; cross-lingual and non-QA memory are untested.

**The operational side, from vendor guidance rather than papers**, pulls the
other way and has to be answered: raw chunks are expensive at read time (they
report pulling ~3,000 tokens to surface one relevant sentence), and retrieval
precision degrades as the store grows — a top-5 recall of 94% at 100 memories
falling to 71% at 10,000 without architectural change. Their recommended
mitigation is retention by class: preferences kept indefinitely, session logs
expiring on the order of 90 days.

## Is any of this the best there is? The leaderboards cannot say

Asked whether the shape above is the best available on this date, the honest
answer is that the field's ranking evidence does not currently support naming a
best. The published leaderboard is high — OMEGA at 95.4% on LongMemEval, Mem0 at
94.4%, EverMemOS at 83.0% overall and 92.3% in its own report — and a May 2026
audit takes most of it apart:

- Ground truth is corrupted: 99 score-corrupting errors in 1,540 questions, a
  6.4% rate, putting the theoretical ceiling near 93.6%.
- The standard LLM judge accepted 62.81% of deliberately wrong-but-topical
  answers.
- The entire LongMemEval-S corpus fits inside a modern context window, so the
  benchmark measures context-window management rather than memory.
- Nearly every headline number is vendor-run, single-seed, without error bars,
  and static rather than interactive.
- Reproduction gaps are not small: EverMemOS's 92.32% reproduced at 38.38% by a
  third party; Zep's 84% was corrected to 75.14% and then to 58.44%.

Independent, multi-dataset evaluation lands in the same place from the other
direction. MemoryBench (11 datasets, 3 domains, 2 languages) reports that
A-Mem, Mem0 and MemoryOS **cannot consistently outperform a naive RAG baseline**
that simply retrieves over all task context and feedback logs. The advantage
those systems show on LoCoMo does not generalise across formats.

What survives this scrutiny is narrow and useful:

1. The strong baseline — keep the material, retrieve over it — is hard to beat,
   and the systems that claim to beat it mostly cannot show it outside their own
   harness.
2. The one comparison that varies **only** the representation, holding retriever,
   reranker, judge and backbone constant, says verbatim beats extraction and a
   union store loses nothing.
3. The recommended practice for anyone choosing is explicit: baseline
   full-context and filesystem-grep on **your own** data, require a candidate to
   exceed that by at least ten points, demand multi-seed numbers with published
   judge prompts, and treat a private adversarially-validated benchmark over real
   use cases as the real gate.

So the proposal below is not "the world's best memory architecture". It is the
baseline that the independent evidence says the elaborate architectures fail to
consistently beat, plus the curated layer this vault already has — and it comes
with the measurement needed to test anything fancier later on our own data.

## What this means for this vault

The two sides agree more than they look. The paper says *keep the source*; the
vendors say *do not let the source dominate retrieval*. Both are satisfied by a
union store with retention: verbatim session evidence retrievable and
provenance-bound, curated pages on top of it, and the raw layer aged out on a
schedule while the compiled layer stays.

That is almost exactly this vault's existing shape. It already has an immutable
sources zone (`knowledge/raw/`, private by default), compiled notes, a 90-day hot
window with immutable BagIt archives beyond it, a hybrid retriever with page-level
diversity in fusion, and typed provenance weighting. What it does not have is the
verbatim layer: sessions reach the vault only through a classifier that decides,
before any question exists, whether the session was worth keeping at all. That is
the write-time commitment the ablation measures as a 16-to-22-point loss, taken to
its limit — not distillation but discard.

Two facts from this vault's own measurement fit the same account:

- The classifier reads the *tail* of a transcript (60,000 characters). On a long
  session the tail is the last stretch of tool traffic; the decisions are earlier
  and outside the window. Giving it a third of the excerpt produced *more*
  promotions (4 of 40 versus 1 of 40) — the window, not the judgement, is doing
  much of the deciding.
- There were no false promotions in either run. The bar is not misaimed; it is
  simply far too high for a system whose failure mode is silence.

## The shape this points to

1. **Keep the session.** A redacted, chunked, provenance-bound copy of the
   session becomes evidence in the vault regardless of tier. No model decides
   whether it is worth keeping, because that decision cannot be made before the
   question is asked.
2. **Keep the judgement, narrow its job.** The classifier stops deciding
   *retention* and decides only *promotion* — whether this session deserves a
   curated page. Its no-false-promotion behaviour is a virtue there.
3. **Retain by class.** Verbatim session evidence ages out on the existing hot
   window; compiled pages do not age out. This is the vendors' answer to store
   growth, and the vault already implements the mechanism.
4. **Measure the same way afterwards.** The stand built for `OPEN-034` scores the
   promotion decision; the retrieval benchmarks score whether an answer can now
   be found. A change that improves memory should move the second without
   worsening the first.

## Sources

- "The Benchmark Theatre: Why Almost Nothing You've Read About Agent Memory
  Scores Is True" — https://essays.bloo-mind.ai/posts/2026-05-20-mem-eval/
- "MemoryBench: A Benchmark for Memory and Continual Learning in LLM Systems" —
  https://arxiv.org/abs/2510.17281
- EverMind, "Best AI Memory Systems in 2026" —
  https://evermind.ai/blogs/top-ai-memory-systems-benchmarked-in-2026
- Mem0, "AI Memory Benchmarks 2026: LoCoMo, LongMemEval & BEAM" —
  https://mem0.ai/blog/ai-memory-benchmarks-in-2026

- "Verbatim Chunks Beat Extracted Artifacts: A Controlled Ablation of Memory
  Representations for Long LLM Conversations" — https://arxiv.org/html/2601.00821v3
- "Connecting the Dots: Benchmarking Reflective Memory in Long-Horizon Dialogue" —
  https://arxiv.org/html/2606.01223
- "Memory Shot for Long-Term Dialogue" — https://arxiv.org/html/2606.28338
- Mem0, "The 2026 Token Optimization Playbook" —
  https://mem0.ai/blog/the-2026-token-optimization-playbook-cut-ai-agent-memory-costs-3%E2%80%934x
- Mem0, "State of AI Agent Memory 2026" — https://mem0.ai/blog/state-of-ai-agent-memory-2026
- "AI Agent Memory in 2026: Mem0 vs Zep vs Letta vs Cognee" —
  https://dev.to/agdex_ai/ai-agent-memory-in-2026-mem0-vs-zep-vs-letta-vs-cognee-a-practical-guide-cfa
- OpenSearch, "Introducing memory retention for agentic memory" —
  https://opensearch.org/blog/introducing-memory-retention-for-agentic-memory-in-opensearch/
