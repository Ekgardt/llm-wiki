# What survives into context, and how to choose it

Dated 2026-08-30. Written before changing the evidence-packing rule, because
the rule is a design decision and three earlier explanations of the same
symptom were each proposed and refuted by measurement.

## The measurement this answers

Read from the compiler's own trace on this vault, n=20:

- Evidence spans reaching the compiler: **1 for nine questions, 2 for five,
  3 for three, 4 for one**. The median is **two of the twelve retrieved**.
- Spans the compiler could not place: **zero**. It places everything given.
- Items the packer dropped for budget: one to four per question.

So the narrowing happens in `query_memory._fitted_selection`, which sheds from
the tail until the rest fit. A session entry is about 10 KB against a
28,672-byte budget.

The rank-one span always survives, which is why single-session questions work.
The two weakest categories need more: on the prompt-level measurement the answer
text reached the model for **2 of 12 multi-session** and **4 of 13 temporal
reasoning** questions.

## What the field does

**The exact problem has a name and a paper this year.** *What Survives Into
Context: A Diagnostic for Budget-Constrained Multi-Hop RAG and When Submodular
Evidence Packing Improves It* (arXiv 2607.00725) frames budgeted packing as the
thing that decides multi-hop answers, and reports that reader-context
construction cast as budgeted monotone submodular maximisation — jointly
optimising relevance, query coverage, representativeness and diversity — beats
an MMR baseline at comparable token cost on HotpotQA.

**Coverage first, then fill.** The practical form is a greedy procedure that
first satisfies coverage by selecting the best span per required source, then
spends the remaining budget with diversity caps. That is the part applicable
here without new machinery.

**Plain diversity is not the answer either.** MMR's diversity objective can
exclude passages carrying complementary evidence and take a steep recall
penalty. So the rule must be "do not spend two slots on one source while
another source has none", not "prefer different things".

**Redundancy is the waste.** Redundant evidence wastes a limited budget,
especially with multiple sources — which is precisely a two-slot budget spent
twice on one session.

**Finer grain is the deeper fix.** SAGE selects evidence below chunk
granularity and remains effective when the budget is smaller than a single
retrieved chunk. Our chunk is a third of the budget, so this applies, but it is
a larger change and is not what this note decides.

**Order matters too, separately.** Models attend more to the start and end of a
context than the middle; a needle at 30–70% depth costs 5–15 points. Not
addressed here, and worth its own measurement.

## The decision

Change `_fitted_selection` so that, when something must go, a span from a page
already represented goes before the only span from another page. Within that
rule the ranking still decides which repeat goes, and where nothing is a repeat
the behaviour is tail-shedding exactly as before.

This is the smallest change that implements coverage-first, needs no new
scoring, no new dependency and no new pass over the corpus, and is aimed at the
two categories the measurement names.

## The cost of being wrong, both ways

**Too much coverage.** Keeping one span from each of several sessions can
displace a second span from the session that actually holds the whole answer —
the recall penalty the MMR literature warns about. A single-session question
whose answer straddles two spans of one page would lose the second. The
measurement guards this: single-session categories are currently the ones that
work, and a drop there is the signal that this went too far.

**Too little coverage** is the present state, and it is measured: two slots,
both spendable on one page, and multi-session answers at 2 of 12.

## What this note does not settle

Whether the change moves the benchmark. Four runs of n=50 on this vault give
accuracies of 0.3542, 0.3200, 0.3469 and 0.2857 — a spread of about seven
hundredths — so a paired re-run showing less than that proves nothing, and a
larger sample is backlog item D1.

## Sources

- [What Survives Into Context: A Diagnostic for Budget-Constrained Multi-Hop RAG and When Submodular Evidence Packing Improves It (arXiv 2607.00725)](https://arxiv.org/abs/2607.00725v1)
- [AdaGReS: Adaptive Greedy Context Selection via Redundancy-Aware Scoring for Token-Budgeted RAG (arXiv 2512.25052)](https://arxiv.org/pdf/2512.25052)
- [SAGE: Selective Attention-Guided Extraction for Token-Efficient Document Indexing (arXiv 2604.15583)](https://arxiv.org/pdf/2604.15583)
- [Principled and Scalable Diversity-Aware Retrieval via Cardinality-Constrained Binary Quadratic Programming (arXiv 2604.02554)](https://arxiv.org/pdf/2604.02554)
- [Practical Code RAG at Scale: Task-Aware Retrieval Design Choices under Compute Budgets (arXiv 2510.20609)](https://arxiv.org/pdf/2510.20609)
- [RAG Chunking Strategies: The 2026 Benchmark Guide — Prem AI](https://www.premai.io/blog/rag-chunking-strategies-the-2026-benchmark-guide/)
- [Long-Context Retrieval 2026: Needle-in-Haystack Test](https://www.digitalapplied.com/blog/long-context-retrieval-needle-in-haystack-2026)
