# A ceiling says which kind of ceiling it is

Date: 2026-09-11. Trigger: audit finding M8. `evidence_graph.MAX_SOURCE_BYTES`
and `MAX_DATABASE_BYTES` (16 GiB), `generation_catalog.MAX_ARTIFACT_BYTES`
(16 GiB) and `MAX_GENERATION_BYTES` (64 GiB) are named like the other
bounds in the product, and the contract says "bounded reads", but none of
them bounds a read: by the time `_normalized_source` compares a source
against 16 GiB the bytes are already in memory, and a 16 GiB artifact
would have failed long before the catalog counted it.

## Sources

1. The real bounds, read 2026-09-11: a source enters a generation through
   the corpus snapshot, which refuses one file over
   `corpus_snapshot.MAX_CORPUS_FILE_BYTES` (= `bounded_io.MAX_KNOWLEDGE_PAGE_BYTES`,
   8 MiB) and a corpus over `MAX_CORPUS_TOTAL_BYTES` (64 MiB); an artifact
   is read to the size the sealed `manifest.json` declares, which the
   catalog verified by hashing (`evidence_graph_builder._declared_manifest_bytes`,
   fixed 2026-09-09 when a 64 MiB constant threw a 158 MB manifest away).
2. `evidence_graph_builder.py:70-78` already names the catalog's constant
   an "absurdity ceiling" for the one artifact it re-exports.
3. Rule 3: a name that reads as a bound while the bound lives elsewhere
   makes the next reader trust the wrong number — the exact shape of the
   2026-09-09 defect in reverse.

## Decision

The values stay: they are the range guards of integer fields and the last
refusal before an artifact is linked into place, and lowering them to the
corpus bounds would make the graph refuse a code source the corpus rules
never governed. What changes is what the code says about them: each
constant is declared under a comment that names it an absurdity ceiling,
says what it guards (a field's range, a mis-typed size, a runaway build)
and points at where the read bound actually lives. The two source checks
in `evidence_graph` raise with a message that says "absurd", not
"bounded". The audit's option 1 (deriving per-artifact bounds from the
manifest) is already the reader's rule; it is not repeated here.

Files: `scripts/evidence_graph.py`, `scripts/generation_catalog.py`,
`CHANGELOG.md`, `docs/AUDIT-2026-09-10-memory-and-retrieval.md`.
