# The keys are indexed beside the turn

Dated 2026-09-16. The fact keys the nightly pass already extracts are joined to candidates
as a separate leg. The paper they come from measured that shape as the worse one. The
research before moving them into the index.

## What is here today

- `fact_keys` extracts short user facts at compile time into a disposable store under
  `cache/fact-keys/`, keyed by the turn's span (`source_path`, `byte_start`, `byte_end`);
  a turn is keyed once (2026-09-09 decision).
- They reach a question through `query_memory._with_keys_leg`: a lexical and dense search
  over the store, merged into the candidate list — a **separate leg**.
- LongMemEval (arXiv:2410.10813) measured both shapes. Key expansion — indexing each round
  **under the facts as well as its text** — raised turn-level recall@10 from 0.692 to 0.784
  with a dense retriever and 0.538 to 0.608 with BM25. Indexing the facts as their own items
  and merging the ranks at query time **lowered** recall@5 from 0.582 to 0.478.
- Dense X Retrieval (arXiv:2312.06648) is the reason the key is short and self-contained,
  and the 2026-09-09 note already states the other half of the rule: what is found must not
  be what is read — a fact a model wrote is never a citation, so the reader still gets the
  turn.

## Practice on this date

- A generated expansion belongs in the index, not in the reader's context: appending
  generated text to a document before encoding helps retrieval and hurts nothing the reader
  sees, as long as the stored text stays the source of truth.
- Keep the two texts apart in the artifact itself rather than by convention, so no later
  caller can hand the keys to a reader by accident.

## The decision

- The generation's search artifact gains one indexed column, `keys`, beside `content`. The
  lexical leg matches over both; every reader keeps reading `content`. The schema version
  becomes `corpus-search/v2`, so an older artifact is simply rebuilt.
- The builder fills that column from the fact-key store by span: a key written for a turn
  whose bytes lie inside a chunk is indexed with that chunk. No store, no column content —
  the build is unchanged for a vault that has never keyed anything.
- The separate leg stays for now; it is measured against the new column in the next stand
  run, and the loser is removed.

Files: `scripts/fact_keys.py`, `scripts/search_memory.py`, `scripts/evidence_graph_builder.py`,
`tests/test_the_keys_are_indexed_beside_the_turn.py`,
`docs/research/2026-09-16-the-keys-are-indexed-beside-the-turn.md`.
