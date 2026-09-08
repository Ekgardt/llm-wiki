# What it takes to win outright, not to catch up — 2026-09-08

Owner's ask: not parity — a clear win. This is the proposal. Nothing here
is done; every item is a proposal with the measured or published reason
behind it, and each is adopted only if one run of 200 beats the 0.035
spread. Sources: `docs/research/2026-09-08-architectures-side-by-side.md`,
run 1 (`cache/benchmarks/longmemeval/second-look-n200-seed101-r1*`),
LongMemEval paper (arXiv:2410.10813), OMEGA blog, Mem0 stand.

## Where the win is

The field's best: 0.954 task-averaged (OMEGA), 0.949 (Mastra), 0.944
(Mem0) — all with a paid GPT reader, all "always answer", none with a
citation anyone can check, none publishing refusal accuracy, tokens and
latency together. A win is: above 0.95 on the official protocol **and**
first on every axis they leave blank — verified citations, correct
refusals, tokens per question, seconds per question, zero cost, local.

## The levers, in order of expected gain

1. **Whole-session delivery.** Fact (run 1): the right session is in the
   twelve candidates 98% of the time, but the sentence with the answer
   reaches the model only 55% of the time; with it we are right 83%,
   without it 64%. Give the model the whole selected session instead of one
   shed 10 KB chunk (`query_memory._shed_one`, `context_budget.pack_context`
   are where the loss happens). Expected alone: 0.75 → ~0.83.
2. **Round-level values with fact keys.** Paper: rounds as the retrieval
   unit, and each round indexed also under the facts extracted from it —
   +9.4% recall, +5.4% accuracy. Extraction runs at compile, in the
   background, on the subscription; capture stays free; raw stays the
   value. Also the cure for 13k tokens: rounds are 300 tokens, chunks are
   2.5k.
3. **Time-aware filtering.** Paper: +6.8–11.3% on temporal questions
   (ours: 0.725). We already resolve the question's dates; add the range as
   a filter on sessions.
4. **Reading strategy.** Paper: chain-of-note plus JSON, up to +10 points;
   OMEGA: a prompt per question shape. We already declare the shape
   (count, sum, latest, preference); one prompt per shape.
5. **Answer mode on the stand.** 28 silences on 186 answerable; 10 had the
   answer in the prompt. On the stand a failed gate reports the uncited
   best answer and says so; the product keeps refusing. Both numbers are
   published. Expected: +0.05–0.08.
6. **Multi-session (0.646).** Fact keys gather every session about an
   entity; whole-session delivery of the top three plus rounds from the
   rest; the second look already widens on counts.
7. **Speed.** 7–8 s search is the cross-encoder on 10 KB pairs. Rerank
   rounds, not chunks (12 × 300 tokens); int8 is already in. Target: search
   under 1.5 s, answer under 20 s. ONNX after the dependency decision.

## Beyond their stand — the axes nobody else can fill

- Refusal accuracy on LongMemEval abstentions, LIT-RAGBench and
  RefusalBench-NQ, beside the accuracy — they have no number here.
- Citation verification rate: share of claims whose cited bytes hash and
  overlap — nobody else has a citation to verify.
- BEAM 1M/10M: Mem0's 0.641/0.486 is the only published number and is
  beatable with whole-session delivery and fact keys.
- LoCoMo with Mem0's own judge prompt, so their 0.925 is comparable.
- Two readers: Claude and Haiku, so nobody can say it is the model.
- The stand, the hypotheses file and per-question answers public; 500
  questions, three seeds, both overall figures.

## Honest ceiling

With the answer in the prompt we are right 83% today. Levers 1–3 move the
answer into the prompt; levers 4–5 move the 83%. I estimate 0.90–0.95
official after all seven; above 0.954 is not promised before it is
measured. What is certain: on citations, refusals, cost and locality no
competitor competes at all.

## Order

Runs 2–3 finish the current decision. Then one lever per run, largest
first, n=200 seed 101, keep if gain > 0.035. Then the pack: three runs,
official protocol, 500 questions, Haiku reader, BEAM, LoCoMo. Nothing is
built without its dated research note; every item here already has one
except levers 2, 3, 4 and 7, which get theirs before code.
