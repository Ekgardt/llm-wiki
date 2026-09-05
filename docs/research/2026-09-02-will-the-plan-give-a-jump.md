# Will the plan give a jump

Dated 2026-09-02. Written to check the plan against the evidence before anyone
spends two weeks on it. The answer changed the plan.

## The short version

**No single step in the plan produces a step change, and the published ablations
say so plainly.** The largest thing on the table is not an engineering step at
all — it is the refusal policy — and a second finding says the benchmark we have
been measuring against is not neutral about it.

## What the ablations actually measure

MemMachine decomposes its own LongMemEval gains, and the ordering is the
opposite of what my earlier note assumed:

```
retrieval depth tuning     +4.2%
context formatting         +2.0%
search prompt design       +1.8%
query bias correction      +1.4%
sentence chunking          +0.8%
```

Retrieval-stage optimisation contributes substantially more than ingestion-stage
change, and chunking — the thing I built yesterday — is the smallest listed
lever at +0.8%.

Two consequences for the plan:

- **Step 4, extracting facts at write time, is an ingestion-stage change.** It
  is not disqualified — MemForest and MemSIF reach 70–80% with it — but the one
  study that isolates the contribution puts ingestion below retrieval. It should
  not be the two-week item it was.
- **Step 1, the answer window, is retrieval depth.** That is the single largest
  lever in the table, and it is configuration rather than construction.

Our own measurement agrees with the direction. The chunking change moved overall
judge accuracy 0.2750 → 0.3229, about +4.8 points, which is larger than +0.8%
because we were starting from outside the sane range rather than tuning inside
it. That is a one-off correction, not a repeatable lever.

## The finding that reframes everything

From a 2026 comparison of RAG systems, two sentences that matter more than the
rest of this note:

> Among RAG systems, accuracy when answering was closely clustered, and
> differences concentrated in the policy governing when not to answer.

> Evaluations that do not price abstention are not neutral; they reward the
> behaviour RAG is deployed to prevent.

LongMemEval scores an abstention as wrong. We abstain on two thirds of
questions, on purpose, because the product's promise is that it does not say
what it cannot show.

So the gap decomposes roughly like this, and every number here is ours:

- accuracy when we answer: **0.77** — in the cluster the sources describe
- coverage: **33%**
- overall on a benchmark that counts silence as error: **0.32**

If coverage rose to 100% and accuracy when answering fell all the way to 0.55,
the benchmark score would be 0.55. That is a bigger move than every engineering
step in the plan combined, and it requires no code — only a decision to answer
where we currently decline.

I am not recommending it. I am recording that it is the only jump-sized lever
that exists, that it is a decision rather than a task, and that taking it trades
away the property the vault was built for.

## What this changes in the plan

1. **Step 1 is promoted.** Retrieval depth is the largest measured lever and
   costs a day. It also answers whether the memory system is the binding
   constraint at all.
2. **Step 4 is demoted** from a two-week build to a measured experiment,
   scheduled after the retrieval-side steps that the ablation ranks above it.
3. **A new item is added: measure on something that prices abstention.**
   LIT-RAGBench carries an explicit Abstention category, and RefusalBench
   evaluates selective refusal directly — where even frontier models fall below
   50% refusal accuracy on multi-document tasks. On a benchmark that scores
   abstention properly, our design should read very differently, and if it does
   not, that is a far more useful finding than another point on LongMemEval.
4. **The engineering steps are honestly re-forecast.** Summing the ablation's
   levers, everything on the retrieval side together is worth on the order of
   ten points, not forty. The plan should say that instead of implying a jump.

## What would honestly constitute a jump

Two things, and only two:

- Changing the refusal policy — decision, not code, roughly +0.2 on this
  benchmark, at a cost the owner has to weigh.
- Being measured on a benchmark that prices abstention — which changes the
  number without changing the system, and is the fairer comparison.

Everything else is accumulation: four to five points at a time, each needing
three runs to prove.

## Sources

- [MemMachine: A Ground-Truth-Preserving Memory System for Personalized AI Agents (arXiv 2604.04853)](https://arxiv.org/html/2604.04853v1) — the component ablation
- [Why RAGs Hallucinate: Penalty-Aware Evaluation with Knowledge-Gap Canaries (arXiv 2608.26385)](https://arxiv.org/html/2608.26385) — accuracy-when-answering clusters; abstention pricing
- [RefusalBench: Generative Evaluation of Selective Refusal — EACL 2026](https://aclanthology.org/2026.eacl-long.321.pdf)
- [LIT-RAGBench: Benchmarking Generator Capabilities in RAG (arXiv 2603.06198)](https://arxiv.org/html/2603.06198v1)
- [Beyond Semantic Relevance: Counterfactual Risk Minimization for Robust RAG (arXiv 2605.01302)](https://arxiv.org/pdf/2605.01302) — coverage/accuracy curve
- [The Confidence Gate Theorem: When Should Ranked Decision Systems Abstain? (arXiv 2603.09947)](https://arxiv.org/pdf/2603.09947)
- Our measurement: `benchmark/longmemeval-unit-n200-r1-judged.json`,
  `benchmark/longmemeval-baseline-n200-r{1,2,3}-judged.json`
