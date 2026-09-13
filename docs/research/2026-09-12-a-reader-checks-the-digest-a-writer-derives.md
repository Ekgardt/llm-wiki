# A reader checks the digest, a writer derives

Dated 2026-09-12. The owner asked how to make answers faster and then handed the
decision over with the same instruction as before: «прими решение согласно
правилу 2 и 4». This is the research, the decision, and the price.

## The measurement first

Cold `get_architecture mode=query` on the installed vault, one fresh process,
profiled: **4.9 s**, of which

- **1.68 s** — `corpus_snapshot._infer_language`, three `findall` passes over the
  full text of each of 3 405 chunks, 10 215 calls;
- **0.87 s** — hashing the generation's artifacts;
- **0.83 s** — SQLite;
- the rest spread thin.

The same call warm: **0.32 s**. The other tool answers in about 2.0 s, warm or
cold, because it has nothing comparable to validate.

All of the 1.68 s, and most of the SQLite time, is one thing:
`validate_generation_fts_artifact` **re-derives every chunk from the stored
sources and compares it row by row** — on every cold open, before any answer.

## Research, current practice on this date

1. Content-addressed stores make the digest the read-time integrity mechanism:
   each record and index object is "serialized deterministically and addressed by
   a cryptographic digest (SHA-256)", and readers verify against that commitment
   rather than rebuilding the content
   ([AGNTCY Agent Directory Service, arXiv 2509.18787](https://arxiv.org/pdf/2509.18787)).
2. Where a read layer wants verifiability, it checks data "against cryptographic
   commitments" and can "verify individual chunks, not just whole blobs" — again
   a digest comparison, not a re-derivation
   ([Stop Trusting, Start Verifying](https://medium.com/shinzo/stop-trusting-start-verifying-building-the-read-layer-blockchain-deserves-91844cf11569)).
3. Deep structural verification is positioned as a maintenance operation even by
   SQLite itself: `PRAGMA integrity_check` "runs a self-check … through a battery
   of tests", is documented as a command an operator runs, and notably "doesn't
   check the data, just the structure"
   ([SQLite integrity check](https://blog.niklasottosson.com/databases/sqlite-check-integrity-and-fix-common-problems/),
   [sqlite-users on its limits](https://sqlite-users.sqlite.narkive.com/DoUeWHPm/how-good-is-pragma-integrity-check)).
   Nobody's read path re-derives the index to trust it.

## What each check actually proves

- The **artifact digest** in the manifest, compared on every validation, proves
  `search.sqlite3` is byte-identical to the file published at build time. The
  manifest itself is hashed and compared against the catalog row.
- The **manifest's versions** — collector, extractor, tokenizer, graph
  extractor — prove the code that produced the rows is the code reading them.
  `_reproducible_by_this_extractor` already refuses the deep check when they
  differ, which is why an older generation is served structurally today.
- The **entry seal** proves nothing in the directory moved while it was open.
- The **row re-derivation** proves one further thing: that re-chunking the very
  same immutable sources with the very same code still produces the very same
  rows. That is a **determinism self-check of our own code**, not a tamper fence:
  a mismatch means our chunker is nondeterministic or buggy, because every input
  is already pinned by the three checks above.

## The decision

**The read path trusts the digest; the deep re-derivation moves to where a
determinism check belongs — publication, the doctor, and the tests.**

By rule 4: it removes about 2.5 s from every cold answer (roughly half), and it
costs no reliability that the other three checks do not already carry. By rule 2
it is what the field does: verify on write, check the commitment on read. Leaving
it on the read path spends 1.68 s of every operator's first question to re-prove
a property of our own code that the build already proved and a test can pin
forever.

Concretely:

- `validate_generation_fts_artifact(..., deep=False)` on the read path: table
  shapes, the metadata row, the generation id, the versions, the chunk count.
- `deep=True` where a generation is **published** and **registered** — the
  moment the rows are created — and in `doctor`, which is the operator's own
  deep check.
- The depth is part of the memo key, so a shallow read can never satisfy a later
  deep registration.

## What is given up, stated plainly

A generation whose `search.sqlite3` is byte-identical to the published file, and
whose versions match, is now served without re-deriving its rows. If our chunker
were nondeterministic, the read path would no longer be the place that notices;
publication, the nightly doctor and the test suite would be. That is the whole
trade, and it is the owner's accepted one — they delegated this decision with
rules 2 and 4 named.

## Also done, needing no decision

`_infer_language` counts characters by building a list of every match. The
counts are only compared, never used, so the three `findall` passes become three
C-level counts with the same results.

Files: `scripts/search_memory.py`, `scripts/generation_catalog.py`,
`scripts/corpus_snapshot.py`, `scripts/doctor.py`,
`docs/research/2026-09-12-a-reader-checks-the-digest-a-writer-derives.md`.
