---
type: decision
status: accepted
date: 2026-09-07
confidence: high
source_authority: user
---
# LanceDB is retired

One-sentence summary: LanceDB is removed from the product — its table was
never built on the installed vault, its only path was the deadline-less
legacy search, and its index was keyed to a different embedder than the
product's — and the legacy dense path keeps its NumPy search.

## Decision

- `scripts/lance_store.py`, `scripts/rebuild_lance_index.py`, the two Lance
  branches in `scripts/search_memory.py`, the `lance_distance` row field in
  `scripts/retrieval.py`, the `lancedb` entry of the `hybrid` extra and
  `cache/lancedb/` are gone.
- `cache/index.sqlite`, `cache/vectors.npy` and `cache/vectors_meta.json`
  remain the legacy caches the contract keeps readable during migration.
- No generation member, catalog entry or runtime root changes.

## Why

Measured on 2026-09-07: `cache/lancedb/` held a manifest and no table, so
`have_lancedb()` had been False since 2026-08-23; the generation path never
named Lance; `lance_store` embedded with `bge-small-en-v1.5` while the product
embeds with `multilingual-e5-small`. The owner asked whether it was used or
needed and left the decision to the rules; the rules say delete what is
useless without breaking anything, and the suites around search, generation
and the benchmark harness pass after the removal.

## Consequences

Anyone who reads "vectors/LanceDB" in an older document reads history. Dense
retrieval on the installed vault is the generation's NumPy vectors, as it has
been since the nightly started building them.

Source: `docs/research/2026-09-07-lancedb-was-never-reached.md`.
Related: [[nightly-builds-generation-vectors-decision]],
[[derived-evidence-generation-decision]].
