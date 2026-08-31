---
type: raw-source
status: active
confidence: high
source_authority: web
date: 2026-08-28
---

# What a rebuild may reuse

One-sentence summary: incremental builders everywhere size their reuse state by
the number of *inputs* and bound it by something the build itself verifies, and
the LLM Wiki generation builder does neither — it puts one JSON row per output
record into a single file bounded by a constant chosen in advance, so on any
real corpus the file passes the constant, is thrown away, and reuse silently
never happens for anyone.

Research for rule 2, ahead of changing how a generation records what the next
generation may reuse. Dated 2026-08-28.

---

## 1. The question this note has to settle

A generation rebuild in this vault reuses nothing. Measured on a copy of this
corpus (839 sources, 38 MB) with the current code, on this machine:

| pass | CPU seconds | rebuilt | reused |
|---|---|---|---|
| from nothing | 637.4 | 839 | 0 |
| nothing changed | 643.1 | 839 | 0 |

The root cause is not the reuse *algorithm* — that is written, tested, and
correct. It is the reuse *state*: `_stored_incremental_manifest` throws the
manifest away when it exceeds `MAX_STORED_INCREMENTAL_MANIFEST_BYTES` (64 MiB),
and on this corpus the manifest is 158,075,010 bytes because it carries one
`record_dependencies` row per record and the generation holds 349,306 records.
Independently confirmed: of 33 generations on disk, **zero** contain
`incremental-manifest.json`.

So the design question is narrow and it is about *storage shape*, not about
dependency tracking:

> Where does an incremental builder put the per-output bookkeeping that lets the
> next build skip work, and what keeps that bookkeeping bounded as the corpus
> grows?

Answering it badly a second time — swapping a 64 MiB cap for a 256 MiB cap — is
the same defect with a longer fuse. What follows is what current practice
actually does.

---

## 2. What the field does

### 2.1 Bazel: two stores, split by what is small and what is large

Bazel's remote cache is deliberately **two** stores, not one: an *action cache*
mapping an action's key to result metadata, and a *content addressable store*
(CAS) holding the output bytes indexed by their own hashes.
([Bazel remote caching](https://bazel.build/remote/caching),
[BuildBuddy](https://www.buildbuddy.io/blog/bazels-remote-caching-and-remote-execution-explained/))
The action key is a digest of the action's metadata and inputs; if any input
changes, the key changes and the action and its dependents rebuild.
([EngFlow, *The Many Caches of Bazel*](https://blog.engflow.com/2024/05/13/the-many-caches-of-bazel/))

The lesson is the split itself. The index that must be consulted to make a
decision is small and holds *keys*; the bulk is addressed by digest and fetched
only for the entries a decision actually selected. Nobody serialises the whole
output inventory into the index.

### 2.2 ccache: a manifest per compilation, not one manifest for the cache

ccache stores, per source compilation, a *manifest* naming the include files
that were read and their hashes; the manifest key is a hash of the source file
plus compiler options plus environment.
([ccache manual](https://ccache.dev/manual/4.13.6.html))
There is no global dependency table. The unit of storage is the unit of reuse —
one input — and lookups touch only the manifests for the inputs in play. Where
ccache does need to scale a shared cache it *shards* it (rendezvous hashing over
named shards) rather than growing one file.

### 2.3 Ninja: per-output dependency records, in a compacted log

Ninja reads compiler-emitted `.d` depfiles, then throws them away and keeps the
information in a binary `.ninja_deps` log, because — in the manual's own words —
"for large projects (and particularly on Windows, where the file system is slow)
loading these dependency files on startup is slow", and the depfile is "only
used as a temporary".
([Ninja manual](https://ninja-build.org/manual.html))

Two things matter here. First, the dependency record is *per output*, appended
as each output is produced — it is never assembled into one document that must
be written whole. Second, Ninja moved *away* from many small files toward one
compact log for read cost, which is the opposite direction from ccache — so the
file/log choice is a performance tradeoff, not a correctness one. What is
constant across both is that the record is never JSON-per-edge and never carries
a materialised transitive closure.

### 2.4 Tantivy and Zoekt: reuse is segment-shaped

Tantivy splits an index into segments; committing new documents "does not modify
existing segments" but adds fresh ones, with periodic merging to stop segment
count exploding.
([Life of a Segment](https://github.com/quickwit-oss/tantivy/wiki/Life-of-a-Segment),
[ARCHITECTURE.md](https://github.com/quickwit-oss/tantivy/blob/main/ARCHITECTURE.md))
Zoekt's delta builds produce shards containing only the changed files, and mark
superseded file versions in older shards with a *tombstone* so results from them
are ignored.
([sourcegraph#29731](https://github.com/sourcegraph/sourcegraph-public-snapshot/issues/29731))

Neither keeps a per-document reuse ledger. Immutability plus an
addressed-by-membership overlay does the work. The relevant lesson for us is
negative: they can avoid a record→owner map because a document belongs to exactly
one shard. Our records do not — a node can be owned by several sources at once,
which is why `test_delete_removes_only_records_exclusive_to_deleted_source`
exists. Segment-style reuse is therefore not available to us; we do need an
explicit ownership map. What we do not need is to also materialise its closure.

### 2.5 Embedding caches: key on the text digest, namespace on the model

LangChain's `CacheBackedEmbeddings` is the settled shape: the text is hashed and
the hash is the cache key, and a `namespace` "is used to avoid collisions with
other caches, for example by setting it to the name of the embedding model
used". The hash function itself is part of the contract (`sha1` by default,
with `sha256`/`sha512`/`blake2b` available), and the documentation warns that
changing the key encoder over an existing cache means starting a new cache
rather than mixing keys.
([LangChain: caching embeddings](https://python.langchain.com/v0.2/docs/how_to/caching_embeddings/),
[API reference](https://python.langchain.com/api_reference/langchain/embeddings/langchain.embeddings.cache.CacheBackedEmbeddings.html))

So: content digest as the key, model identity in the namespace. Nothing more
exotic is standard, and nothing less is safe.

---

## 3. What this vault already has, and what it is missing

The interesting finding is that **the hard part is already built and already
bounded** — it is only stored wrongly.

- A chunk's identity is already a content digest.
  `corpus_snapshot.canonical_chunk_id` hashes `{extractor, parent, path, range,
  sha256}` where `sha256` is the digest of the chunk's own bytes, and
  `chunk.text` is exactly those bytes decoded. So equal `chunk_id` implies equal
  `span_sha256` implies — by SHA-256 preimage resistance — equal text.
- Every published generation already writes those digests next to its matrix:
  `vectors.json` carries `chunk_ids`, `model_id`, `model_revision`,
  `dimensions`, `schema_version`. On the live vault that is 3,211 chunk digests
  against a 4.9 MB `vectors.npy`.

That is a complete LangChain-style embedding cache, already on disk, already
immutable, already garbage-collected by the catalog's own generation retention —
and nothing reads it. `build_generation_vectors_if_available` re-encodes every
chunk of every build, which the profile puts at 595.1 s of a 721.2 s pass
(82.5%).

The record side is the one that needs a new shape:

| part of `incremental-manifest.json` | grows with | read by the reuse path? |
|---|---|---|
| `version`, `reuse_config` | nothing | yes |
| `sources[]` minus `records` | number of sources | yes |
| `sources[].records` | number of **records** | yes (`_wanted_record_ids`) |
| `record_dependencies` | number of **records** | **no** — grep finds no reader in `scripts/`; only two assertions in `tests/test_evidence_graph_incremental.py` |

`record_dependencies` is ~85% of the file and is consumed by nobody. It is also
fully derivable from what remains: a row's `source_ids` is the owners plus their
`source_dependencies`, and its `status` is whether any owner was rebuilt.

---

## 4. The shape chosen, and why it stays bounded

Two changes, and the second one is the point.

**Cut what is O(records) and unread.** `record_dependencies` becomes a bounded,
deterministic prefix plus an exact `record_dependencies_total`. It is ~85% of
the file, no reader in `scripts/` consumes it, and every row is derivable from
what remains: `source_ids` is the owners plus their `source_dependencies`, and
`status` is whether any owner was rebuilt. Keeping 127 MB of unread audit rows
inside an immutable generation, for every generation, is the unbounded shape
being removed — not a thing to relocate.

**Stop bounding the rest with a constant.** `sources[].records` is the load-
bearing half: the record→owner map cannot be derived, because `node`,
`assertion` and `observation` carry no `source_id` in `evidence.sqlite3`, and
ownership is a *set* — a node can belong to several sources at once, which is
what `test_delete_removes_only_records_exclusive_to_deleted_source` protects. It
is irreducibly O(records), about 20 MB here.

So the fix is not to find a bigger number. It is to stop having a number at all:

- **Reading** is bounded by the size the sealed `manifest.json` declares for the
  `incremental-manifest.json` artifact. That is not a hint —
  `generation_catalog._validate_generation` has already hashed every artifact
  against the manifest and refused a wrong size or digest before the reuse path
  reads a byte (`generation_catalog.py:1300-1327`). The bound is therefore
  self-describing and verified, and a larger corpus simply declares a larger
  number.
- **Storing** keeps a refusal, but its ceiling is now
  `generation_catalog.MAX_ARTIFACT_BYTES` (16 GiB) — the largest single artifact
  the catalog will register at all. A manifest past it belongs to a generation
  that could not be published anyway, so the "manifest dropped, reuse lost"
  branch stops being the only branch and becomes unreachable in practice.

This is the same idea as ccache's per-compilation manifest key and Bazel's
action-cache/CAS split — the index that decides is verified and addressed, and
the bulk is fetched against that verified description — expressed with the
verification this codebase already performs rather than with a new store.

### The out-of-line sidecar was built first, and rejected — for two measured reasons

The obvious shape from §2, and the one this note originally specified, was a
separate `record-membership.json` per generation with the manifest naming it by
digest. It was implemented and it worked (manifest 4.3 MB, sidecar 19.6 MB,
`reused_sources` 839). It was then removed, because it bought nothing that the
declared-size bound does not, and cost two things that matter:

1. **It could not live inside the generation.**
   `generation_catalog._V2_OPTIONAL_ARTIFACTS` is a closed allowlist, and
   validation additionally requires the files on disk to equal the manifest's
   artifact list plus `manifest.json` (`generation_catalog.py:216,1542`). A new
   file there — listed or not — makes the generation invalid. The sidecar
   therefore had to sit outside the sealed directory, which meant re-earning
   integrity through a digest chain and re-earning lifecycle through a
   garbage-collector that deleted sidecars of pruned generations. Both are
   solvable, and both are complexity paid for nothing once the bound inside the
   sealed file is already self-describing.
2. **It broke readers that legitimately depend on the current shape.** Three
   tests in `tests/test_generation_maintenance.py` read
   `manifest["sources"][…]["records"]` directly to assert ownership
   partitioning. Moving membership out of line is a contract change for them,
   and the contract they assert — that a manifest entry states what its source
   owns — is a reasonable one.

The measured cost of keeping membership inline is one 20 MB JSON parse per
rebuild, roughly 0.3 s against a pass that was 600 s. That is the right trade.

### What is *not* claimed

Membership is still O(records) on disk: about 20 MB per generation, against the
~200 MB `evidence.sqlite3` each generation already carries, so roughly 10% on
top, retained exactly as long as the catalog retains its generations. There is
no encoding trick that removes this — 349,306 SHA-256 identities are 11 MB even
as raw bytes. The claim is only that nothing in the path refuses to store or
read it because of a number chosen in advance.

## 5. What invalidates a reused vector, stated exactly

A cached row is used for a chunk only when **all** of these hold:

- `chunk_id` is equal. This already binds the extractor version, the owning
  source id, the source path, the byte range, and the digest of the chunk's own
  bytes.
- `model_id` is equal (`intfloat/multilingual-e5-small` today).
- `model_revision` is equal (`614241f622f53c4eeff9890bdc4f31cfecc418b3`).
- `dimensions` is equal (384).
- the parent's vector `schema_version` is `corpus-vectors/v1`.
- the parent matrix loads as float32 with shape `(len(chunk_ids), dimensions)`.

The prefix E5 requires (`passage: `) is a constant of the passage side, so it
does not need a key of its own; if it ever varies it must join `model_id` in the
namespace.

## 6. The measured limit on "byte-identical", found before building anything

The gate for this work says a reused vector must be byte-identical to what a full
build would have produced. Measured on 400 real chunks from this vault's live
generation, before any code was changed:

| comparison | bitwise equal | max abs difference |
|---|---|---|
| same texts, same order, encoded twice | **yes** | 0 |
| 50 texts alone vs the same 50 inside a 400-batch | **no** (3 of 50 rows) | 8.57e-08 |
| 1 text alone vs the same text inside a 400-batch | **no** | 7.82e-08 |

So the embedder is deterministic for a fixed batch composition and *not*
deterministic across re-batching — sentence-transformers sorts and pads by
length, and changing which texts share a batch changes the reduction order in
float32.

The consequence has to be said plainly rather than engineered around: **a full
build's `vectors.npy` is already not byte-identical to another full build's**
whenever the corpus changes, because the batches differ. "Byte-identical to what
a full build would have produced" is therefore not a property the current full
build has either, and it cannot be the acceptance test for reuse. The honest
pair of claims that *can* be proved, and are proved in the implementation's
tests and measurements, is:

1. a reused row is byte-identical to the parent generation's row for that chunk,
   and that chunk's bytes are identical — so reuse introduces no error of its
   own; and
2. incremental-vs-full disagreement stays within the same float32 rounding band
   as full-vs-full disagreement — reuse is no further from a full build than a
   full build is from itself.

The record half has no such limit: records are exact values, and
incremental-vs-full there is compared for exact equality.

## 7. What shipped, measured

Paired on one copy of this vault's corpus (839 sources, 38 MB), same machine
(4 vCPU), `doctor._build_or_refresh_generation` end to end, CPU seconds
including children:

| pass | before | after | rebuilt → reused (after) |
|---|---|---|---|
| from nothing | 637.4 | 568.7 | 839 → 0 |
| nothing changed | 643.1 | **3.9** | short-circuited: `status: current` |
| one note added | 703.7 | **104.6** | 1 → 839 |
| one note edited | 829.0 | **123.5** | 475 → 365 |

The `before` figures for the last two ran against other work on the same
machine and are inflated; `nothing changed` at 643.1 is the clean baseline and
is the fair comparison.

`nothing changed` collapsing from ten minutes to four seconds is a *second*
consequence of the same defect, not a second fix. `doctor._parent_is_current`
compares the parent's `workspace_manifest_sha256`, and
`_parent_workspace_manifest` reads it out of the parent's incremental manifest —
which no generation had. The "nothing to do" fast path was therefore
unreachable, and every idle pass did a full rebuild it should never have
started.

Manifest, on the same corpus: **158,075,010 bytes and discarded → 23,815,492
bytes and stored** (20,196,211 of it the irreducible membership map,
3,618,742 the bounded audit sample of 10,000 rows out of 352,092).

A caveat worth stating because it is the trap this note exists to avoid:
23.8 MB is *under* the old 64 MiB constant, so cutting the audit rows alone
would have appeared to fix this today — and would have broken again at roughly
2.7× the current corpus. That is why the constant was replaced with the sealed
manifest's declared size rather than raised.

### Correctness, compared rather than asserted

Same corpus built twice — once incrementally from a parent (365 sources reused),
once full from nothing — then compared row by row:

| table | rows | identical |
|---|---|---|
| node | 28,846 | yes |
| occurrence | 25,354 | yes |
| assertion | 70,666 | yes |
| evidence | 148,946 | yes |
| observation | 78,280 | yes |
| dependency | 0 | yes |
| source | 840 | yes |

For vectors, 3,213 of 3,214 chunks were reused, and the reused rows are
**bitwise equal** to the parent's rows for those chunks. Incremental vs full is
*not* bitwise equal: 50 of 3,214 rows differ, max |Δ| 1.341e-07, mean 3.593e-10,
worst per-row cosine 0.9999999404.

That residue was attributed rather than excused. Re-encoding exactly those 50
chunk texts in a fresh batch and comparing against the *full* build gives max
|Δ| 1.155e-07, and disagrees with the full build on 27 of the 50 rows. So a full
build does not reproduce itself under different batching either, by the same
order of magnitude — the delta is the model's float32 re-batching noise, and
reuse contributes none of it.

### Found, not fixed

Editing one knowledge page rebuilt 475 of 840 sources, while *adding* one page
rebuilt 1. The asymmetry is entirely in the pre-existing invalidation policy,
which simply never ran before: `_semantic_changes` only inspects sources in
`changed`, so an added source can never be a semantic change, and any semantic
change makes `_workspace_invalidated` rebuild the whole workspace-sensitive set
— 418 sources here, plus transitive dependents. Two questions follow and neither
is answered here, because changing invalidation policy is a separate decision:
whether a knowledge-page edit should invalidate the code workspace at all, and
whether an added source should be able to invalidate its dependents.

## Source / Evidence

- Bazel remote caching — <https://bazel.build/remote/caching>
- BuildBuddy, *Bazel's Remote Caching and Remote Execution Explained* —
  <https://www.buildbuddy.io/blog/bazels-remote-caching-and-remote-execution-explained/>
- EngFlow, *The Many Caches of Bazel* (2024-05-13) —
  <https://blog.engflow.com/2024/05/13/the-many-caches-of-bazel/>
- ccache manual 4.13.6 — <https://ccache.dev/manual/4.13.6.html>
- Ninja manual, "Depfile support" / deps log —
  <https://ninja-build.org/manual.html>
- Tantivy, *Life of a Segment* —
  <https://github.com/quickwit-oss/tantivy/wiki/Life-of-a-Segment>
- Tantivy `ARCHITECTURE.md` —
  <https://github.com/quickwit-oss/tantivy/blob/main/ARCHITECTURE.md>
- Zoekt incremental indexing / delta shards —
  <https://github.com/sourcegraph/sourcegraph-public-snapshot/issues/29731>
- LangChain, *Caching embeddings* —
  <https://python.langchain.com/v0.2/docs/how_to/caching_embeddings/>
- LangChain `CacheBackedEmbeddings` API reference —
  <https://python.langchain.com/api_reference/langchain/embeddings/langchain.embeddings.cache.CacheBackedEmbeddings.html>
- In-repository measurement: `scripts/evidence_graph_builder.py:60,64`
  (`MAX_STORED_INCREMENTAL_MANIFEST_BYTES`),
  `scripts/generation_catalog.py:216,1542` (closed artifact allowlist and the
  files-on-disk equality rule), `scripts/corpus_snapshot.py:495`
  (`canonical_chunk_id`), `scripts/search_memory.py:565` (`_vector_metadata`).
- Prior measurement: `docs/research/2026-08-28-bounded-watching-without-a-daemon.md`
  and `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md` → `CODE-04`.

## Related

- [[knowledge/notes/derived-evidence-generation-decision]]
- [[knowledge/notes/nightly-builds-generation-vectors-decision]]
