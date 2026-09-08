# Whole entries into the window, and a fan-out before counting — 2026-09-08

Tasks 1 and 2 of `docs/TASKS-to-100-2026-09-08.md`. This note is the
research the change is built on; the measurement comes after, one run of
200 against the 0.035 spread.

## What is actually lost between retrieval and the model

Measured today on run 1 and on a probe vault:

- A session is written as one daily entry under one heading
  (`## [time] session_end | id`), and the corpus splitter cuts it into
  pieces of at most 4 096 bytes (`corpus_snapshot.MAX_SPAN_BYTES`), every
  piece carrying the same `heading_ancestry`. A 9 KB session is three
  pieces; a 20 KB one, five.
- Retrieval ranks pieces, not sessions. Twelve candidates are twelve
  pieces. The context compiler turns a piece into an L2 item that is the
  piece itself unless the whole heading subtree fits in 2 000 characters
  (`context_compiler.DEFAULT_LARGE_PARENT_SUBTREE_CHARS`), which a session
  almost never does. So the model reads one 4 KB slice of a session whose
  answer sentence may be in another slice.
- The packer does not drop evidence: `packer_dropped` (median 7 of 12) is
  page overviews (L1), and `evidence_missed` is 0 on every question. All
  twelve pieces reach the model. The loss is which pieces were chosen.
- Every failed multi-session count in run 1 has candidates from the
  labelled sessions (`answer_sessions_retrieved` ≥ labelled in 12 of 16),
  and is still one instance short — the piece that names the instance was
  not the piece retrieved.

## What the field does about it

- LongMemEval authors (arXiv:2410.10813, CP1): index at round level, and
  keys finer than the value — small units to find, larger units to read.
- Emergence AI (https://www.emergence.ai/blog/sota-on-longmemeval-with-rag):
  "matches on individual conversation turns but retrieves entire sessions",
  sessions ranked by NDCG of their turns; 82.4% with GPT-4o against the
  oracle's 82.4%, 86% with their best reader.
- Verbatim beats extracted (arXiv:2601.00821): 82–85% for verbatim chunks
  against 65–72% for facts and summaries — the value must stay raw text.
- Google AI Mode fan-out (https://searchengineland.com/guide/query-fan-out;
  60 000-query study at https://websearchapi.ai/blog/what-is-query-fan-out-and-insights):
  one question becomes 5–11 longer, more concrete sub-queries run in
  parallel and merged; completeness comes from overlap, not from
  classifying the question.
- Perplexity (https://www.langchain.com/breakoutagents/perplexity): plan
  first, then execute; the first retrieval layer is tuned for recall and
  the cross-encoder adds precision.
- Yandex Spectrum: when the reading of a query is ambiguous, serve several
  readings rather than choosing one.

## Decision

1. **Whole entries.** When a piece is selected, every piece of the same
   entry — same source and same `heading_ancestry` — goes into the window
   with it, in byte order, placed right after the piece that earned the
   entry its rank. Entries therefore keep retrieval's order (best-ranked
   entry first) and read top to bottom. The budget still sheds from the
   tail, so the lowest-ranked entry loses its pieces first. This is
   Emergence's "match on turns, retrieve sessions" inside our own
   compiler, and the authors' CP1 without changing the index. For an
   ordinary note the entry is its heading section, which is what a
   reader would want to see whole anyway. Off switch for the sweep:
   `LLMWIKI_QA_WHOLE_ENTRIES=0`.
2. **Fan-out before counting.** When the answer declared a count or a sum,
   one short model call turns the question and the items already found
   into at most five concrete sub-queries (Google's fan-out); each is
   searched without the cross-encoder (recall first, Perplexity's first
   layer); the new pieces join the first candidates behind them, capped at
   twenty-four; and the answer is generated once more with a counting rule
   beside the question: list every instance with its date and citation,
   then count. The existing edge rule and entity clustering stay. The
   second answer is adopted only when it answered — Spectrum's "serve both"
   reduced to "keep the better-evidenced one that still verifies".
3. **What does not change.** The index, the schema, the citation gates, the
   refusal contract, the capture path. No new module beyond the fan-out
   helpers in `aggregation_pass.py`; no dependency.

## Cost, stated before measuring

Whole entries: more bytes per question (twelve pieces become roughly eight
entries of two to five pieces; 45 KB → about 80 KB of prompt), zero extra
calls. Fan-out: only on questions whose answer counted or summed — one
short call, up to five reranker-free searches (about 1 s each warm), one
more answer call. On LongMemEval that is 41% of questions; on the product
it is the rare question that counts.

## Rule of decision

One run of 200, seed 101, each lever separately against the second-look
baseline (runs 1–3). Kept if judge accuracy rises by more than 0.035;
whole entries also has to keep tokens per question under twice the
baseline.
