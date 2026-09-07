# LanceDB was never reached — 2026-09-07

## The question

The audit of 2026-09-07 listed LanceDB as legacy. The owner asked whether it
is used at all and whether it is needed, and left the decision to the rules.

## Facts, measured on the installed vault

- `cache/lancedb/` held a `__manifest` directory and nothing else — 28 KB, no
  `pages_vec` table — so `lance_store.have_lancedb()` returned False on every
  query since the directory was created on 2026-08-23.
- The only callers were `search_memory._legacy_dense_hits` and
  `_legacy_vector_results`. The first returns before reaching Lance whenever a
  deadline is given, and every current caller gives one; the second is the
  deadline-less legacy search. The generation path
  (`evidence_graph_builder`, `retrieval`) never named Lance.
- `lance_store.EMBEDDING_MODEL` was `BAAI/bge-small-en-v1.5`; the product
  embeds with `intfloat/multilingual-e5-small`. A Lance table, had one been
  built, would have been searched with a query vector from a different model.
- `rebuild_lance_index.build_lance_generation` wrote a `lance/rows.json`
  member into generations — but only from one test fixture; no product code
  called it.
- The optional dependency `lancedb>=0.20` sat in the `hybrid` extra; the
  installed environment carried `lancedb 0.34.0` and its two namespace
  packages for nothing.

## What the operating contract required

`CLAUDE.md` §1 kept `cache/lancedb/` "readable during migration" until
"installed-vault migration evidence makes that safe". The evidence is the
above: there was nothing to migrate, because nothing had ever been written,
and the nightly has built generation vectors since
`nightly-builds-generation-vectors-decision`.

## Decision

Retire LanceDB entirely: the store module, the rebuild script, the two
search branches, the `lance_distance` row field nothing produced, the tests
that tested only the store, the dependency, and the cache directory. The
legacy dense path keeps its NumPy brute-force search unchanged. Recorded as
`knowledge/notes/retire-lancedb-decision.md`.

Not a quality change: no answer on the installed vault could have differed,
because the branch never executed. Verified by the suites around search,
generation and the benchmark harness after the removal.
