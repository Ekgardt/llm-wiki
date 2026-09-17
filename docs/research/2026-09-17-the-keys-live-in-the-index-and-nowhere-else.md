# The keys live in the index and nowhere else

Dated 2026-09-17. Third audit, retrieval M3 and L6 together with generations M4: who reads the
fact keys the nightly pays for, and should the product give them vectors as the stand does.
The decision was delegated by the owner; the research before it.

Files: `scripts/fact_keys.py`, `scripts/query_memory.py`, `benchmark/longmemeval_vault.py`,
`tests/test_a_fact_key_points_at_the_turn.py`,
`tests/test_a_key_reply_is_read_as_written.py`,
`tests/test_the_keys_have_one_reader_and_a_turn_is_not_asked_forever.py`,
`tests/test_an_error_is_not_an_answer.py`

## What was found

- Generations M4 said nothing in the product reads the keys. That was true on the audited
  commit only because the key lookup raised on every call. Since `260295a` the keys are the
  twenty-third column of the search table, the lexical leg of every generation search matches
  over it, and the build copies the store into it (`evidence_graph_builder._nightly_keys`).
  The nightly spend has a reader.
- A second reader is still wired: `query_memory._with_keys_leg` searches the key store as its
  own leg (lexical, and dense when vectors exist) and merges the turns into the candidates.
- Retrieval M3: the nightly calls `key_turns(..., encode=None, ...)`, so every product key has
  a NULL vector and the dense half of that leg never returns a row; the stand passes an
  encoder, so it measured a leg the product does not have. `_resident_encoder` was written to
  close that gap and nothing calls it.
- Retrieval L6: a turn the reply did not cover stays pending, and pending order is chunk
  order, so a batch that fails every night is asked first every night and spends the budget
  before any new turn is reached. Nothing bounds how many nights a turn is asked.

## Practice on this date

- LongMemEval (Wu et al., arXiv:2410.10813, section 5.3 and table 3, fetched 2026-09-17)
  measured both shapes. The expansion is concatenated with the value to form the key at
  indexing time ("K = V + fact"): with rounds as values that gave recall@5 0.644, against
  0.530 for the facts indexed alone ("K = fact"), and the paper reports "an average improvement
  of 9.4% in recall@k and 5.4% in final accuracy" for the merged key. Keeping the facts as
  separate items and merging ranks after retrieval is the shape it measured as worse.
- The same point from the side of the scores: the separate leg ranks by a BM25 over a table
  of short keys and a cosine over key vectors, and its turns enter the candidate list by
  vote, outside the fused and fitted order every other candidate gets
  (`docs/research/2026-09-17-one-table-one-scale-for-the-keys.md`).
- Bounded retry: a unit of work that fails repeatedly is retried a small fixed number of times
  and then set aside, so it cannot starve the work behind it — the dead-letter rule of every
  queue, and the one `memory_queue` already follows in this repository.

## The decision

- The keys have one reader: the `keys` column of the generation's search table. That is the
  shape the paper measured as the good one, it is already in production, and it needs no
  vectors — so the question "should product keys have vectors" is answered by removing the
  only code that could read them.
- Removed: `query_memory._with_keys_leg`; `fact_keys.search`, `_votes`, `_turns_of`,
  `KeyStore.lexical`, `KeyStore.dense`, `_blob`, `_encoded`, `_resident_encoder`, the `encode`
  parameter of `key_turns`, the `vector` column and the `key_fts` table of a new store. An
  existing store keeps its old columns; the writer names the columns it fills, and the store
  is disposable.
- The stand keys the turns the way the nightly does (no encoder), so it measures the product.
- The nightly step stays: it has a reader, a turn is keyed once, and its cost is bounded by
  the step budget.
- A turn is asked at most three nights (`MAX_ATTEMPTS`). Attempts are counted in the store;
  turns never asked go first, and a turn that used up its attempts is left to be found by its
  own text, which the index always carries.
- The tail of generations M4 — model-written text inside a sealed artifact — is not a broken
  contract: the artifact is sealed by its own digest, the validator rebuilds and compares only
  the twenty-two source-derived columns, and the generation stays disposable and rebuildable.
  What is true and is now said in the module: a rebuild after the key store was deleted
  carries different keys, so two builds of one snapshot need not be byte-identical.
- No stand run was made (none is permitted). The refit of `lane_score` and any measurement of
  the column against no column wait for the owner's permission.
