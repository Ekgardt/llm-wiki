# A chunker that changes changes its version

Dated 2026-09-17. Third audit, finding G-M1. The research before the fix.

Files: `scripts/corpus_snapshot.py`,
`tests/test_a_chunker_that_changes_changes_its_version.py`.

## What was found

- `corpus_snapshot.EXTRACTOR_VERSION` has been `markdown-heading-extractor/v3` since
  2026-09-08. On 2026-09-16 commits `4b1cb26` and `124f277` changed where a conversation is
  cut (`MIN_USER_ROUND_BYTES`, `_round_bound`, `_kept_cut`): a user turn of 48-159 bytes now
  begins its own chunk.
- Measured on one fixed conversation, the tree before `4b1cb26` against today's, both
  naming themselves `v3`: spans `(0,278) (278,674) (674,1014) (1014,1336)` became
  `(0,278) (278,600) (600,674) (674,1014) (1014,1336)`.
- Everything that asks "was this generation made by today's extractor" reads that string:
  `search_memory._reproducible_by_this_extractor` (deep validation re-derives the chunks and
  compares), `doctor._corpus_extraction_state` (health), `doctor._parent_matches_versions`
  (whether the nightly may answer `current`), and `doctor._maintenance_extractor_identity`
  (what an incremental build may reuse). With the string unchanged, a generation built
  before the change is called reproducible and fails re-derivation, the nightly keeps the old
  chunking for as long as the corpus is unchanged, and the doctor says healthy. This is the
  2026-09-10 incident again (`docs/research/2026-09-10-a-generation-outlives-its-extractor.md`);
  the guard built then works, it was simply not told.
- `260295a` changed the search table (`corpus-search/v2`, the `keys` column). That one has
  its own version and did bump it; nothing to fix there.

## Practice on this date

- Semantic Versioning 2.0.0, item 3: "Once a versioned package has been released, the
  contents of that version MUST NOT be modified. Any modifications MUST be released as a new
  version." (https://semver.org/). A version string that names a derivation rule is a cache
  key; a rule that changes under the same key poisons every cache keyed on it.
- A rule that lives only in a reviewer's memory is forgotten — twice here in one week. The
  durable form is a test that holds the rule's output beside its name.

## The decision

- `EXTRACTOR_VERSION` becomes `markdown-heading-extractor/v4`. The next nightly sees the
  active generation as made by another extractor and rebuilds it once; vectors are reused by
  chunk digest, so only moved chunks are encoded again. A code generation is rebuilt in full,
  not incrementally, the next time its content changes.
- A test pins the digest of the chunks of one fixed conversation and one fixed page under
  the version's name. Changing the chunker without changing the version fails it; changing
  the version requires writing the new pin. The class of the defect, not the instance.
- The fact-key store's stale `span_sha256` rows (audit M4) are not touched here.
