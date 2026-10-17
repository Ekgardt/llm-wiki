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


2026-10-05 streaming follow-up (candidate, not installed): the earlier sealed
whole-byte implementation is the baseline. The user-approved transient work
scope now permits an owned private stream-copy of the same existing JSON/NPY
artifacts. A digest covers exactly the copied bytes, and source identities are
checked. Parent and active reader matrices map only those owned copies. This
preserves the earlier immutable-input guarantee even if an original is truncated;
opening a bare parent memmap would have lost that guarantee and was rejected.
Owned scratch and metadata lookups close and disappear on success or refusal.

Fresh independent primary sources checked 2026-10-05: NumPy 2.5
[open_memmap](https://numpy.org/doc/stable/reference/generated/numpy.lib.format.open_memmap.html)
and [memmap](https://numpy.org/doc/stable/reference/generated/numpy.memmap.html),
Python 3.10.22 [JSON decoder](https://docs.python.org/3.10/library/json.html),
and SQLite [CREATE TABLE](https://sqlite.org/lang_createtable.html) and
[temporary storage](https://sqlite.org/tempfiles.html). Minimum Python 3.10
APIs are retained. A second permanent database, global caches, new settings,
changed vector formats, whole JSON arrays, and producer-only mapping were rejected.
The parent FTS chunk ID is UNINDEXED, so per-ID lookup there would be quadratic.
A private, owned temporary SQLite lookup indexes IDs and stores replayable arrays;
it is not published, retained coordination, or knowledge authority.

The writer holds the existing measured 256-text work batch and writes the same
float32 NPY format. JSON array order and literal UTF-8 are unchanged. Reuse
checks exact text/span identity and the model revision, including the historical
missing-extractor exact-ID fallback. The reader validates all parallel arrays
against successive FTS rows. Dense scoring holds one existing 4096-row numerical
work block and the caller-derived result pool for each of the existing two
source tiers; ranked admission agrees with the full earlier order. Invalid JSON,
repeated IDs, changed seals, cancellation, and expired clocks remain refusals.
Two unused whole-byte helper functions were removed; old research above remains
historical evidence of the replaced implementation.

Controlled resource proof used 100,001 real canonical Markdown chunks and a
384-dimensional numerical encoder. This encoder proves working-data/schema
behavior, not actual model speed or answer quality. An earlier candidate build
took 11.350 s; complete first/repeated metadata reads took 2.844/2.832 s, with
identical all-array digests and owned scratch removed. Its peak RSS was
230,348 KiB, including mapped output pages; this is not a constant-RSS claim.
JSON was 18,700,560 bytes and NPY 153,601,664 bytes. These measurements precede
the final active-reader private-copy repair and remain historical measurements,
not its full-search price. Final full FTS/vector/reuse/cold/warm resource proof
and actual model qualification are still required before installation.

### 2026-10-05: matched streaming build, sealed readers and reuse

The isolated matched workload contains two actual Markdown sources and 100,001
canonical physical chunks, each source below the retained per-source guard. The
original `4518cc5f` FTS admission rejects this exact snapshot at the global row
ceiling. The approved streaming candidate builds it, validates every FTS row
against the captured physical sources, reads sealed vector artifacts twice, and
reuses the sealed parent with byte-identical JSON and NPY artifacts. The numeric
384-dimensional encoder is controlled working data; this is neither an embedding
quality benchmark nor an actual model call.

On the final measured search source `a8335255b66f22455a9120570aa4cd948904f2feb72043d53e4333fb962dd12a`,
collection took 1.900 s, FTS construction 2.983 s, vector construction 11.035 s,
strict FTS validation 3.705 s, dense first/repeated reads 6.572/6.158 s, and parent
reuse 15.426 s. Peak process RSS was 391,664 KiB. These are observed durations on
a shared machine, not isolated latency, OS-cold measurements or a speedup claim.
The private dense stage returned the same 15 candidates both times; the public
result-admission stage was not part of this resource probe. Its reuse probe proves
artifact parity, while the separate unchanged-257-chunk regression proves no
re-encoding. No whole-vault generation or semantic answer quality is claimed.

A broader consumer run revealed a real independent-connection regression: the
streaming identity reader had omitted the historical `sqlite3.Row` initialization.
A caller with a deadline opens a separate connection, so dense ranking received
tuples and fell back to base search. The common initialization is restored. The
new independent-connection guard first failed, then passed with the existing
public deadline regression and related readers: 29 tests passed. The previous
broader result, 176 passed and one failed, remains retained rather than rewritten
as a successful full run. That older ranking suite also loaded its local Torch
reranker; it must not be reported as zero model activity.

Actual Lizard and AST checks cover all 153 changed callables in the shared search
source and owned vector tests, plus seven diagnostic handlers: maximum CCN 5,
maximum two `if` statements and two branch/loop levels. Ruff passes. Deployment,
full real-corpus generation, operator attention and actual semantic model cost
remain separate qualification gates. The existing per-source limits and metadata
byte budget remain explicit audit work; this change does not justify them.


## Streaming metadata driver calls — 2026-10-05

Profiling the retained real 126,298-row vector artifact identified 505,205 Python-level SQLite calls while decoding its four identity arrays and four repeated ordered identity walks. Instrumented 14-second queries timed out; those profiled durations are diagnostic costs, not unprofiled latency measurements.

The selected implementation sends each existing array to SQLite `executemany` through a lazy generator and compares all four identity fields in one ordered walk. It adds no resident cache, materialized full array, arbitrary batch limit, schema, dependency, environment setting, or persistent runtime path. Existing scalar/string/count/duplicate checks, digest-sealed artifact copies, cancellation and deadlines remain in place. A genuine pre-fix regression run failed two tests; five corruption controls already passed. After the change, all 124 related tests passed. The new test file has a measured shard weight; all 402 changed/new callables, including nested functions and lambdas, meet CCN <= 5, at most two `if` statements and nesting <= 2. Ruff passed.

Primary sources checked on 2026-10-05: [Python 3.10 sqlite3](https://docs.python.org/3.10/library/sqlite3.html), [SQLite INSERT](https://www.sqlite.org/lang_insert.html), and [NumPy memmap](https://numpy.org/doc/stable/reference/generated/numpy.memmap.html). A resident metadata cache, full in-memory arrays and arbitrary batch sizes were considered and rejected: the simpler driver-level stream removes repeated crossings while retaining the existing ownership and validation boundary.

Two unprofiled dense-only queries with the real installed E5 model returned 48 hits each within the unchanged 14-second deadline: 11.195766s and 10.133824s. Source SHA256: `b8be1ffdb260a1177d34f9ba3b6e0f6fd867e14ce91292f05ec35a22f59b2e00`. Peak RSS was 1,398,820 KiB; CPU costs were 17.473260s and 14.850735s. Paid provider calls: zero; local model token counts: unknown. The fixture's graph tables are empty, so these measurements qualify the dense artifact reader only. They do not prove a complete generation, ordinary MCP hybrid admission, an isolated speedup, installation, or audit closure.

Evidence: `logs/audit-2026-10-05-step7-vector-metadata-stream-original-red.log`, `logs/audit-2026-10-05-step7-vector-metadata-broad.log`, `logs/audit-2026-10-05-step7-streaming-sixth-all-current-complexity.json`, and `logs/audit-2026-10-05-step7-dense-metadata-stream-unprofiled.jsonl`. The earlier failed and interrupted runs remain part of the full cost record. A new full regression run and installation are still pending.
