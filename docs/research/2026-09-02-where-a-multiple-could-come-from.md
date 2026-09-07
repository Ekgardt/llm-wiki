# Where a multiple could come from

Dated 2026-09-02. Second pass, because the first produced tenths and the owner
asked for a step change.

## The finding I did not expect

Our answer budget is **28 672 bytes, about 7 000 tokens**, and the comment in
`benchmark/longmemeval_vault.py` says why: it was chosen to match Mem0's
"under 7 000 tokens" claim so the cost comparison stays fair. The product's own
stock budget is smaller still — **8 192 tokens**.

The reader we hand that to accepts two hundred thousand. We are compressing to
roughly three per cent of what it can read, deliberately, to win a token
comparison.

There is a 2026 paper about exactly this failure mode, *Fixed RAG Compression
Collapses Measured Reader Scaling*: a constant compression budget applied
regardless of context complexity breaks the scaling that a more capable reader
should give you, because the fixed constraint stops it from using the extra
retrieved information at all. Its recommendation is adaptive compression that
scales with reader capacity.

So one of our two headline numbers — accuracy — is being held down to protect
the other — tokens. That was a deliberate, documented choice. It has never been
priced, and it is the cheapest thing on this list to test: one arm per budget,
and the stand answers it.

## What the reader is worth

MemForest reports **70.4% with a 4B answer model and 79.8% with a 30B** one, on
the same memory system. Nine points from the reader alone.

And the control that says memory still matters: a closed-book check with all
retrieved evidence removed put every reader tested **below 4%** on
LongMemEval-S. Retrieval is not the part to abandon; it is the part that makes
any of it possible.

## Sleep-time compute

The idea is to move work off the query path into idle time: a heavier model
reasons over accumulated context before any question arrives and leaves a better
representation behind, and a lighter model answers online.

Reported: **up to 18% accuracy gain** on reasoning tasks, **2.5× lower cost per
query** when amortised across related queries, and about **5× less test-time
compute** for equal accuracy. The stated limit is honest and applies to us: it
helps most when future questions are somewhat predictable from existing context.

We already run a nightly pass that compiles the day. This is that pass doing
more than compiling — the same slot, more work in it.

## Memory in the weights, and why not

Temp-LoRA and StreamAdapter train a temporary adapter on the context to
internalise it. The 2026 production picture is blunt about where this belongs:
RAG is the default adaptation layer, and LoRA is reached for when the failure is
reasoning *shape or style*, not knowledge — with a weekly adapter over recent
high-signal interactions as the common cadence. Distilling a specific context
into weights biases the model and causes catastrophic forgetting of general
instruction following.

Our failure is knowledge, not style. This is the one option I would rule out
rather than schedule.

## The five candidates, with what is known about each

**A. Stop compressing to 7 000 tokens.** Cheapest to test, no new machinery, no
contract change. Unknown gain here; the mechanism is documented and our budget
is 3% of the reader's window. Costs tokens per query, directly against the
token-sparing rule — so it is a trade the owner has to price, not a free win.

**B. Extract facts at write time.** Reference systems: MemForest 70.4–79.8%,
MemSIF +2.29–8.79% over the strongest baseline. Large build; needs a decision
page. Counter-evidence exists and is cited: *Storage Is Not Memory* reaches 87.8
keeping events verbatim, so this must be **derived** state beside the record,
not a replacement for it.

**C. Sleep-time compute in the nightly pass.** Up to 18% accuracy, 2.5× cheaper
per query, 5× less compute at equal accuracy. Fits a slot we already run. Helps
only where questions are predictable from context.

**D. Two-pass retrieval.** The mechanism biological recall actually uses, and
the one our two weak categories need. Estimated from our own numbers at roughly
+0.13 overall. Small build, no contract change.

**E. A larger answer model.** Nine points in a published ablation, on the same
memory. Costs money per query and is the least interesting engineering, but it
is the honest control: if it moves us a lot, the memory system was not the
binding constraint.

## What I would do, and in what order

A and E first, together, because both are configuration rather than
construction and between them they tell us whether the memory system is even the
binding constraint. If accuracy climbs steeply with budget and reader, the
answer is not a new architecture; if it does not, B and C are justified and D is
cheap regardless.

I am not claiming any of these reaches 0.88. What I am claiming is that we have
been measuring our memory system through a 3% window and a refusal policy, and
neither of those has been priced.

## Sources

- [Fixed RAG Compression Collapses Measured Reader Scaling (arXiv 2606.21807)](https://arxiv.org/pdf/2606.21807)
- [Sleep-time Compute: Beyond Inference Scaling at Test-time (arXiv 2504.13171)](https://arxiv.org/html/2504.13171v1)
- [Sleep-time Compute — Letta](https://www.letta.com/blog/sleep-time-compute/)
- [MemForest: Hierarchical Temporal Indexing (arXiv 2605.23986)](https://arxiv.org/html/2605.23986)
- [MemSIF: Structured Interactions to Dual-Track Fact Memory (arXiv 2608.01742)](https://arxiv.org/html/2608.01742)
- [MemReader: From Passive to Active Extraction (arXiv 2604.07877)](https://arxiv.org/pdf/2604.07877)
- [Benchmarking Memoria on LongMemEval: clear reader separation](https://medium.com/@matrixorigin-database/benchmarking-memoria-on-longmemeval-strong-memory-retrieval-clear-reader-separation-ee6c89c75d76)
- [Real-Time Learning in LLMs (2026): Online Learning Methods Explained](https://futureagi.com/blog/real-time-learning-in-large-language-models-llms/)
- [Understanding LoRA as Knowledge Memory: An Empirical Analysis (arXiv 2603.01097)](https://arxiv.org/html/2603.01097v5)
- Our budget and its rationale: `benchmark/longmemeval_vault.py::ANSWER_INPUT_BUDGET`
