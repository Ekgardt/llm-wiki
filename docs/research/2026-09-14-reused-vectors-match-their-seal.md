# Reused vectors match their seal

Dated 2026-09-14. A guess at the end of `docs/AUDIT-2026-09-14-2.md`, confirmed by a
read-only audit today. The research before the fix.

## What was found

- An incremental build takes unchanged chunks' rows from the parent generation
  (`search_memory._reusable_vector_rows` → `_parent_vector_metadata`,
  `_rows_by_chunk_id`, `_loaded_parent_matrix`). It checks that `vectors.json` and
  `vectors.npy` exist, the metadata size, the model identity, unique chunk ids and the
  matrix shape and dtype. It never compares the two files with the SHA-256 the parent's
  sealed `manifest.json` records for them (`artifacts[]`: `path`, `sha256`, `size`,
  checked on this machine's latest generation today).
- The reader checks those seals (`_sealed_generation` → `_artifact_seals` →
  `_sealed_file`). The builder does not: a `vectors.npy` damaged or replaced with a
  same-shape float32 matrix is copied row by row into the new generation, which then
  seals fresh digests over the wrong vectors and hides the damage for good.
- Rows are keyed by chunk id, a hash of the chunk's content, so "reused by path" is not
  the problem; the unverified bytes are.
- The code graph: `_reusable_vector_rows` ← `_built_generation_vectors` ←
  `build_generation_vectors_if_available` ← `evidence_graph_builder`
  (`_vector_reuse_source` only checks the parent directory exists).

## Practice on this date

- Content-addressed caches verify the digest of what they reuse at the point of reuse;
  a cache hit whose bytes do not match their recorded digest is a miss (the rule git
  applies to objects and `uv`/pip apply to hashed artifacts,
  [pip secure installs, hash-checking mode](https://pip.pypa.io/en/stable/topics/secure-installs/)).

## The decision

- The builder reads the parent's `manifest.json`, takes the recorded `sha256` for
  `vectors.json` and `vectors.npy`, and reuses rows only when the bytes it reads hash to
  those values; the matrix is loaded from the same bytes it hashed, so the checked bytes
  are the used bytes. Any mismatch or missing seal means no reuse — the build embeds
  every chunk, as every refusal here already does.

Files: `scripts/search_memory.py`, `tests/test_reused_vectors_match_their_seal.py`,
`docs/research/2026-09-14-reused-vectors-match-their-seal.md`.
