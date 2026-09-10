# Fact keys beside the turn — 2026-09-09

Task 7 of `docs/TASKS-to-100-2026-09-08.md`, the last lever that does not
need a run to build.

## What is known

- LongMemEval (arXiv:2410.10813, CP2, "key expansion"): index each round
  under the user facts extracted from it as well as under its text —
  +9.4% recall@k, +5.4% accuracy. The facts are keys; the round stays the
  value the reader gets.
- Dense X Retrieval (arXiv:2312.06648): propositions as the retrieval unit
  outperform passages; the key is a short, self-contained statement.
- Verbatim beats extracted for *reading* (arXiv:2601.00821): the extracted
  fact must never replace the turn, only point at it.
- Our own laws: no model call at capture; derived state lives under
  `cache/` and is regenerable; a citation is a byte span of a file the
  user wrote, so an extracted fact can never be cited.

## Decision

1. **Extraction at compile, not capture.** A nightly step reads the user
   turns of daily entries that have no keys yet, hands them to the provider
   in batches of twenty-five, and asks for up to five short facts per turn
   ("I own a 5-piece Pearl Export drum set", "I see Dr. Smith every
   week"). One call per batch; an entry is keyed once, by the hash of its
   turn's bytes, so a rebuilt generation keeps its keys.
2. **A derived store, beside the generation, never inside a citation.**
   Keys live in `cache/fact-keys/keys.sqlite3`: one row per key with the
   turn's path, byte range and span hash, an FTS5 index over the key text,
   and the key's vector from the retrieval encoder. The generation's FTS
   and vectors are untouched, so every artifact contract and validator
   stays exactly as it is. The store is disposable: delete it and the
   nightly step rebuilds it.
3. **A keys leg at query time.** Beside the lexical, dense and graph legs
   the answer asks the key store for the turns whose keys match the
   question — lexically, and by cosine when the encoder is resident — and
   the turns join the candidates by vote, resolved to their chunks by path
   and byte range. A key never reaches the model; the turn it points at
   does, with its partner, pruned as any other.
4. **The stand may extract too.** `LLMWIKI_BENCH_FACT_KEYS=1` runs the
   same extraction on a question's haystack before retrieval — about ten
   batched calls a question — so the lever is measurable; off by default
   because it is the stand's cost, not the product's.

## Cost

Nightly: one call per twenty-five user turns of new entries. Query: one
FTS query and, with the encoder resident, one small matrix product.

## Rule of decision

Two arms of 200 with the stand extracting and not; kept if recall of the
labelled sessions and accuracy rise by more than the spread.
