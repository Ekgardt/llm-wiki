# A memory stand that measures retrieval

Dated 2026-09-14. The owner asked why vectors and the reranker gave no gain on
LongMemEval, then decided: rebuild the stand first, fix answer composition
second. This note is the research for the rebuild.

## What was wrong in my own reading first

I told the owner the stand retrieves "practically everything". It does not.
`retrieved` in a result row is `len(rows)`, the number of candidate chunks handed
to the answer — **12** (`QA_CANDIDATES`) — out of a snapshot of about **467
chunks** in about 11 daily files. Retrieval is not saturated. I also compared
`answer_sessions_retrieved` with `answer_sessions_labelled` as if both counted
sessions; the first counts *rows* that name a labelled session, the second counts
*sessions*, so that comparison meant nothing.

## What the data does say

- On the 500-question lexical run, accuracy falls with the number of sessions the
  answer needs: **0.906** at one (149 questions), **0.878** at two (196),
  **0.781** at three (32), **0.471** at four (17), **0.375** at five (8),
  **0.333** at six or more (3).
- The answer budget is not the binding limit: the stand runs at
  `ANSWER_INPUT_BUDGET = 122 880`, and its own 2026-09-02 measurement found that
  "all twelve candidates already fit. The window had stopped being the binding
  constraint; the count is."
- On 206 questions answered both ways, vectors plus reranker moved 19 verdicts —
  9 up, 10 down — and left 34 wrong in both.
- The first chunk from an answer session is at rank 1 in 175 of 206 questions
  lexically and 190 of 206 with the full path; that single-hit signal is near its
  ceiling either way.

So the stand cannot currently answer the question that matters: **how many of the
sessions and evidence turns a question needs actually reached the reader.** It
records a first-hit rank and a row count, and neither distinguishes "one of four
sessions found" from "all four found".

## The dataset can say it

LongMemEval ships the labels needed: `answer_session_ids` per question, and a
`has_answer: true` flag on the individual turns that carry the evidence (question
one of `longmemeval_s`: 54 haystack sessions, 1 answer session, 2 flagged turns).

## Practice on this date

1. **LongMemEval itself scores retrieval separately from answers**, with Recall@k
   and NDCG@k over its evidence labels, using session-level evidence matching for
   `longmemeval_s` and turn-level units, with session-level probes as diagnostics
   ([LongMemEval benchmark](https://www.emergentmind.com/topics/longmemeval-benchmark),
   [LongMemEval paper](https://arxiv.org/pdf/2410.10813),
   [how scoring targets shape memory benchmarks](https://arxiv.org/pdf/2605.24060)).
2. **For multi-hop questions, "any" recall is the wrong number.** Recall@k is the
   fraction of gold evidence retrieved in the top k; Set-Recall@k measures coverage
   of the gold evidence set; and **acc@k** is the fraction of questions where *all*
   supporting facts are within the top k — and "recall is often more predictive of
   downstream answerability because missing a required evidence passage prevents
   the reader from deriving the answer"
   ([Multi-hop question answering survey](https://arxiv.org/pdf/2204.09140),
   [retrieving a set, not independent passages](https://arxiv.org/pdf/2607.05712),
   [context precision vs recall](https://oneuptime.com/blog/post/2026-08-31-context-precision-vs-context-recall-rag/view)).
3. **Depth is an arm to measure, not a constant to trust.** This stand's own note
   already cites MemMachine's ablation: retrieval depth is the largest single lever
   at +4.2% against +0.8% for chunking.

## The decision

Four changes to the stand; the product is not touched.

1. **Coverage, at session and turn level.** A new module,
   `benchmark/longmemeval_coverage.py`, computes per question: labelled answer
   sessions and how many distinct ones the retrieved rows name; flagged evidence
   turns and how many of them appear in the retrieved text; and from those,
   session recall, turn recall, and the all-or-nothing flags (acc@k) for sessions
   and turns. Pure functions over the question and the rows, so they are tested
   without a vault.
2. **Coverage at the depth the reader got, and deeper.** Every run records coverage
   at 12, the candidates the answer actually sees. A retrieval-only run also records
   it at 24 and 48 from one deeper retrieval, so depth becomes a measured arm.
3. **A retrieval-only mode.** `run_longmemeval.py --retrieval-only` builds each
   question's vault, retrieves, records coverage and skips the reader and the
   judge. It spends no provider tokens and no judge hour, which makes the full 500
   affordable after every retrieval change.
4. **The report separates the two failures.** Coverage is aggregated by question
   type; for runs with answers, a wrong answer with every evidence turn in hand is
   a reader failure, and a wrong answer with evidence missing is a retrieval
   failure. That split is what decides whether the next change goes into search or
   into answer composition.

Why not the alternatives:

- **Keep reading `answer_session_rank`.** It is a first-hit signal, and it is at
  190 of 206 already: it cannot see a multi-session question that found one of
  four sessions.
- **Widen `QA_CANDIDATES` and rerun the 500 with the reader.** That changes the
  thing measured before the stand can measure it, and costs five hours and a judge
  hour per arm on a machine that has already killed two such runs for memory.
- **Match evidence by the gold answer string.** `gold_in_candidates` exists and its
  own comment says it over-counts ("a gold answer of `20` matches any candidate
  containing that substring"); the dataset's turn labels make guessing unnecessary.

## Bounds and cost

A turn counts as covered when any window of its text — 80 characters, stepping
by 40, whitespace collapsed, case folded — appears in a retrieved row's text;
turns of 80 characters or fewer are matched whole. A prefix alone was the first
design and it undercounts: rows carry chunks of about 250 characters, and a chunk
boundary can hand over the middle of a turn without its first line.

## First measurement, two questions, retrieval-only

Run from the worktree code with the main checkout's environment, one question at
a time:

- `6d550036` (multi-session, 4 answer sessions, 4 evidence turns): at 12, 3 of 4
  sessions and 1 of 4 turns; at 24 and 48, 4 of 4 sessions and still 1 of 4
  turns.
- `gpt4_d84a3211` (multi-session, 4 and 4): 4 of 4 sessions at every depth and
  **0 of 4 turns** even at 48. Checked by hand: all four evidence turns exist in
  the snapshot's chunks, rendering preserves their text, retrieved rows carry real
  chunk content (one of 2 444 characters, the rest 240-286), and not one 80-char
  window of any evidence turn is in the top 48.

So the right session reaching the reader and the right sentence reaching it are
different events, and on these two questions the second fails where the first
succeeds — which the old first-hit signal could not show. The retrieval-only run still
loads the embedder and the reranker, measured at 1.2 GB and 2.6 GB, so it runs at
concurrency 1.

Files: `benchmark/longmemeval_coverage.py`, `benchmark/longmemeval_vault.py`,
`benchmark/run_longmemeval.py`, `tests/test_longmemeval_coverage.py`,
`docs/research/2026-09-14-a-memory-stand-that-measures-retrieval.md`.
