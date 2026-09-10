# Tokens are a retrieval-unit problem — 2026-09-08

The owner, on seeing 19k–58k prompt tokens per question after tasks 1–6:
"catastrophically much", and on the same-day cut (three whole entries, a
second pass that reads only the cited spans and the new pieces): "looks like
a crutch". This note says which part is a crutch, which is not, and what
the researched answer is.

## What the tokens buy today, and why

- The retrieval unit is a piece of up to 4 096 bytes (`MAX_SPAN_BYTES`).
  Twelve candidates are 12 × ~3.5 KB ≈ 45 KB ≈ 11k tokens before any
  entry comes in whole. The model reads 4 KB to find one sentence because
  ranking cannot point at the sentence.
- The cross-encoder that could rank finer has never run on the stand
  (`optional_stage_timeout` on every row; 20 pairs × 4 KB do not fit in
  12 s). Ranking is reciprocal rank fusion of lexical and dense scores over
  4 KB pieces.
- Whole entries (task 2) add the rest of a session — 5–15 KB each — so
  the sentence that sits in the next piece reaches the model. Right, but
  paid in the coarsest possible currency.
- The second pass re-sent the entire first window. That was waste, not
  design; iterative retrieval systems (FLARE, arXiv:2305.06983; Self-RAG)
  carry the draft forward and read only what is new. Reading the cited
  spans plus the new pieces is that rule, and stays.
- "Three whole entries" is a number chosen under pressure. It is the
  crutch. It stays only until the unit below replaces it.

## What the field does about it

- LongMemEval (arXiv:2410.10813, CP1): index at *round* level; smaller
  units find better and cost less to read. Their best reading setup pairs
  round-level retrieval with key expansion.
- Emergence AI (https://www.emergence.ai/blog/sota-on-longmemeval-with-rag):
  match on turns, retrieve sessions, rank sessions by the NDCG of their
  turns — the session is the reading unit only for the sessions whose
  turns won.
- Supermemory (https://supermemory.ai/research/longmembench/): 720 added
  tokens at 95% — small memories, recall@15. The unit is the whole trick.
- HORMA (arXiv:2606.11680): a navigator picks the minimal sufficient
  context from summaries linked to raw trajectories; 22% of baseline
  tokens at equal or better accuracy.
- MemReranker-4B (arXiv:2605.06132): reranking memory items beats
  bge-reranker; the items are small.

## Against the 2026-09-02 decision, read again

`docs/research/2026-09-02-the-unit-of-retrieval.md` chose 4 KB paragraph
pieces over turns, quoting sources that turn-level context is fragmentary
and session-level retrieval outperforms turn-level "due to richer
contexts". Both statements are about what the model *reads*. The
LongMemEval authors' result is about what retrieval *finds*: rounds are
found better than sessions, and the value handed to the reader can be
larger than the key. The two decisions are compatible once the unit of
finding and the unit of reading are separated, which the 09-02 note did
not do and this one does: find rounds, read the round with its neighbours,
and the whole entry for the one that ranked first.

## The design that follows the rules

1. **Retrieval unit: the round.** Split a session entry at turn boundaries
   (`**user:**` / `**assistant:**` lines), each round a chunk of about
   0.3–1.5 KB carrying the same `heading_ancestry` as today; a round longer
   than 4 KB still splits at paragraphs. The index, FTS5 and vectors work
   unchanged; the citation gates verify bytes and do not care.
2. **Rank entries by their rounds.** Candidates are rounds; an entry's
   rank is the best of its rounds (Emergence's NDCG, reduced to max).
   Delivery: every retrieved round, its neighbouring rounds either side
   (the question and its answer), and the whole entry only for the one
   entry ranked first. Expected: 12 rounds × ~0.8 KB × 3 ≈ 30 KB before
   the first entry, half of today's first pass, and the sentence is in it
   because the round is the sentence's unit.
3. **The reranker on rounds.** 20 pairs of 0.8 KB at two threads is
   about a second; the 12-second bound stops being reached and the stage
   finally scores. Measured against no reranker at all; the loser goes.
4. **Whole entries become an exception,** not a default; the number three
   is removed with them.

## Rule of decision

Two arms of 200 after tasks 1–6 are measured: rounds with the reranker
running, rounds without. Kept if accuracy is within the spread of the
tasks 1–6 arm and prompt tokens per question fall below the run-1 figure
(13.4k). The target on the owner's metric, verified-correct answers per
thousand tokens, is stated before the run: at least 2× run 1.
