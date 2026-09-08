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

## Part 2 — the owner said the seven are not enough. What makes it decisive

The seven levers bring us to where the field already is. A decisive lead
cannot be made on LongMemEval alone: the top three sit within one point of
each other at 0.95, and 1.00 is the ceiling. The lead has to come from
(a) the half of our architecture the stand does not use yet, (b) a loop
nobody else can run because nobody else verifies, and (c) the stands and
axes where the field is weak or absent.

8. **Use both layers on the stand.** The stand builds a disposable vault of
   raw entries and never compiles. The product has a second layer — compiled
   pages with supersession, `valid_from`/`valid_to`, confidence, authority —
   built in the background. Run compile on the stand's vault; answer
   knowledge-update and preference questions from the page (the current
   value, with the superseded one named), detail questions from raw. This
   is the architecture we built; today we measure half of it. Target:
   knowledge-update 0.72 → 0.95 (Supermemory 0.99 on this type).
9. **Claim-guided retrieval (closed loop).** We are the only system whose
   answer is verified against the bytes. When a claim fails its citation
   gate, the claim text itself is the best query there is — it names the
   entity and the value. Re-retrieve with the failed claim, re-verify, at
   most once. Converts the ten silences-with-evidence and part of the
   nineteen wrong answers into verified answers. Unique: nobody else has a
   failed claim to search with.
10. **Decompose multi-session questions.** The paper and every leader lose
    on multi-session (Mastra 0.87 ceiling, OMEGA 0.83, ours 0.65). One
    LLM call splits the question into sub-questions, each retrieved
    separately, then the aggregation pass counts. Agentic retrieval is
    the known cure; we have the aggregation pass already.
11. **Win the stands where the field is weak.** BEAM 10M: Mem0 0.486, no
    one else publishes. LoCoMo: Zep/Mem0 0.92–0.95, but on their own
    judges. Refusal: nobody. Multi-language: nobody publishes; our
    citation gate already works across scripts. Publish all four beside
    LongMemEval; the lead there can be tens of points, not tenths.
12. **The efficiency figure.** The owner's metric is not tokens saved but
    correct answers per token. Define and publish "verified correct per
    1k tokens" and "seconds per correct answer". Round-level values put us
    near 4k tokens; at 0.95 that is 2× Mem0, 7× Mastra, and every correct
    answer carries a checkable citation, which none of theirs do.
13. **Fully offline arm.** Every >0.94 result uses a paid GPT reader. An
    Ollama-only run (local reader, local embedder, local reranker) at
    0.90 would be a number nobody else has at all. Cost: zero. Rule 4 in
    one line.
14. **Reproducibility as a feature.** Seeded, per-question answers,
    hypothesis file, open stand, three seeds, 500 questions, two judges'
    protocols. The competitor numbers are single runs on private stands;
    ours are re-runnable by anyone. This is how "nobody can dispute it"
    becomes true.

Targets after 1–14, stated as targets, not promises: official ≥ 0.96,
multi-session ≥ 0.90, knowledge-update ≥ 0.95, BEAM-10M ≥ 0.70, refusal
≥ 0.90 with over-refusal ≤ 0.05, tokens ≤ 4k, search ≤ 1 s, offline arm
≥ 0.90. Each lever gets its own dated research note before code, and its
own run against the spread. Levers 8 and 9 are the ones I would build
first after runs 2–3: they use what only we have.
