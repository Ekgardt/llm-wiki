# A generation outlives its extractor — 2026-09-10

**Seen.** Measuring the default reranker on the live vault, every `search`
took 34–49 s and answered `effective_mode=BASE`, `signals_used=('lexical',)`,
`fallback_reason=generation_unavailable`; two of three Russian questions
returned no rows at all. Profiled: 40 of the 49 s were six calls of
`generation_catalog._validate_generation` (three attempts of `_get_active`,
each validating the active generation and its parent), and 34 of those 40
were `search_memory.validate_generation_fts_artifact` re-deriving 11,046
chunks from the 1,113 stored sources and comparing them with the 11,044
stored rows. The comparison fails at row 9,272: the current code ends a
chunk at byte 5,213 where the stored one ends at 6,307.

**Cause.** Commit 0bc0943 (2026-09-08, "a conversation is found by the
round; only the top entry comes whole") changed `_retrieval_spans` and
bumped `corpus_snapshot.EXTRACTOR_VERSION` to
`markdown-heading-extractor/v3`. The active generation was built on
2026-09-07 by v2 and its manifest says so. The validator passes the
manifest's version into `canonical_retrieval_chunks`, but that argument
only labels the chunk id — the spans are always the current algorithm's.
So a v2 generation checked by v3 code is "semantically invalid", the
catalog refuses it, retrieval drops to the legacy BM25 index, and the
verdict is not remembered: `_validate_databases_once` memoises success
only, so the six validations recur on every search, in every process.
The nightly would have rebuilt it (`_parent_matches_identity` compares the
extractor version) but has failed since 2026-09-07 on an unrelated defect
fixed today, so the vault has answered lexical-only since 2026-09-08.

**What the check is for.** Artifact bytes are already proven by the
manifest's SHA-256 (`_scan_artifacts`); the FTS content check guards
against a builder that wrote rows its own sources do not produce. That
guard is only meaningful for the builder this code is: an older
extractor's rows cannot be reproduced by a newer one, and asking the
question yields a wrong answer, not a cautious one. The owner accepted on
2026-09-05 that the index may lag its sources
(`knowledge/notes/an-index-may-lag-decision.md`); lagging its extractor is
the same lag one version further up.

**Decision.**
1. `validate_generation_fts_artifact` derives the expected chunks only
   when the manifest's `extractor_version` is this code's; otherwise it
   validates what it can — schema, metadata against the manifest, stored
   count, every stored row well-formed — and the generation serves. The
   nightly rebuilds it on the version mismatch, as it already does.
2. `_validate_databases_once` remembers a failed verdict under the same
   hashed identity it remembers a success under, and re-raises it; the
   same bytes get the same answer without paying for it again.
3. Nothing else: no schema field, no new state, no change to the builder.

**Cost afterwards, measured on the live vault.** See the figures appended
below once the change is in.

**Files.** `scripts/search_memory.py`, `scripts/generation_catalog.py`,
`tests/test_generation_catalog.py`, `CHANGELOG.md`, `docs/ISSUES-2026-09-10.md`.
