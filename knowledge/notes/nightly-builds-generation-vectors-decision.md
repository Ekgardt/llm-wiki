---
type: decision
status: active
confidence: high
source_authority: user
date: 2026-08-23
---

# The Nightly Builds The Vectors Of The Generation It Publishes

One-sentence summary: semantic retrieval stops being code that only tests
exercise — the maintenance pass that publishes a generation also builds its
vectors, so a question in any language can reach a page written in another.

## Context

The owner asked for a system that does not depend on the language of the
question. Measured on the installed vault, a Russian question returns nothing
while its English translation returns ten results. Two legs could answer, and
neither did.

The lexical leg is SQLite FTS5 and ranks by token overlap; it cannot cross
languages and is not asked to. The dense leg can, and this vault carries a
complete implementation of it: a vector builder
(`search_memory.build_generation_numpy_vectors`), catalog validation that a
generation declaring `vector_state: complete` really carries its vector files,
and a search path that uses them. Every one of those is covered by tests.

Nothing in the product calls the builder. Searching the repository for its name
returns eight test call sites and the definition. Every generation this runtime
has ever published carries `vector_state: absent`, and the live vault reports
exactly that. The legacy per-vault vector cache is closed on the same day it is
needed: `_legacy_dense_hits` returns `None` whenever a deadline is set, which is
every ordinary query.

So the semantic leg has never run in any installed vault. It is not a missing
feature; it is a finished feature that nothing turned on.

## Decision

The bounded maintenance pass that builds and publishes an immutable generation
also builds that generation's vectors, and publishes `vector_state: complete`.

The embedder is the one named by `scripts/embedding_model.py` — multilingual by
decision, so the vectors it writes are what makes the language of the question
stop mattering.

Three properties are kept exactly as they are today:

- **Immutability.** Vectors are written into the generation directory before it
  is registered, never into an activated one. A generation is complete or it is
  not published.
- **Failing closed.** When the embedding model is unavailable — not installed,
  not downloadable, out of time budget — the pass publishes the generation with
  `vector_state: absent`, exactly as it does now, and says so. A vault without
  the optional dependency keeps working with lexical search alone.
- **The bounded budget.** Building vectors happens inside the same deadline and
  the same maintenance fence. Running out of time defers the vectors, not the
  generation.

## Why this shape

The alternative was to open the legacy path — the mutable `cache/vectors.npy`
that `_legacy_dense_hits` refuses under a deadline. That would be quicker and
wrong: the legacy cache is mutable, unfenced and unverified, and the deliberate
refusal exists because it has no bounded implementation. The generation path is
the one this product decided on: immutable, catalog-validated, activated by
compare-and-set, and already able to state whether its vectors are complete,
stale or absent.

Vectors also belong to the same snapshot as the graph and the search index they
sit beside. Building them anywhere else would let them disagree with the corpus
they claim to describe, which is the defect the generation design exists to
prevent.

## Evidence

- Live vault before the change: `generation` reports `vector_state: absent`;
  `cache/vectors.npy` does not exist; three Russian questions return 0, 0 and 1
  results while their English equivalents return 10 each.
- `build_generation_numpy_vectors` has no non-test caller in the repository.
- The encoder change that makes this worth doing is measured in
  `docs/research/2026-08-23-retrieval-must-not-depend-on-the-language.md`: the
  multilingual model puts the right page first on all three Russian questions at
  0.77–0.86, where the English-only model scored everything 0.43–0.52 and picked
  one wrong page.

## Open questions

Whether a vault whose generation carries stale vectors should refuse them or use
them with a warning. Today the catalog can express `stale`; nothing produces it.

Whether the reranker should be enabled at all. If it ever is, it must be
multilingual, or it reorders cross-language hits badly and undoes this.

## Related
- [[knowledge/notes/rerank-tier-ordering-decision]] — what the cross-encoder scored outranks what it never read, and why the tail is kept at all.

- [[knowledge/notes/derived-evidence-generation-decision]] — the immutable
  generation contract this stays inside.
- [[knowledge/notes/self-resolving-health-findings-decision]] — the same rule
  applied to health: a report must describe what is true now.
- [[knowledge/notes/session-evidence-retention-decision]] — links to this page.
