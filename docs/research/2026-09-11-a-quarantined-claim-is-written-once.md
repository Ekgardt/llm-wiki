# A quarantined claim is written once

Date: 2026-09-11. Trigger: the nightly pass of 2026-09-11 (`logs/nightly-2026-09-11.md`)
ended `compile: FAILED — FileExistsError: knowledge/inbox/claims/21015ca3…-44efd…md`.

## What happened

- Transaction `541017dc…` (operation `compile-quarantine:c68bb9…`) committed that
  candidate on 2026-09-07 19:26; the file on disk still hashes to the
  `after_hash` the transaction recorded (`25e024d4…`), so nothing changed it.
- A quarantined batch publishes candidates only and leaves its daily pending
  (`_committed_outcome`, issue #26.2). The next compile planned the same daily
  again. The model proposed the same claim over the same evidence, so the
  candidate path — named after the claim's fingerprint and the digest of its id
  and evidence — was the same path. The action key differed (the rest of the
  plan differed), so the operation id differed, the create change met the
  existing file, and `_require_capture_matches_kind` raised `FileExistsError`.
  The batch was lost and the nightly counted a failure.
- Any recompile of a quarantined daily whose model output repeats one claim
  fails the same way; the contradiction pipeline's own writer (`_commit`) has
  the same shape.

## Sources

1. Content-addressed stores treat a write of an object that already exists as
   a no-op: git checks the object path before writing
   (https://dev.to/arnabsantra2004/the-git-filesystem-recreating-the-content-addressable-database-444h,
   https://github.blog/open-source/git/gits-database-internals-i-packed-object-store/),
   and blob stores delete the temporary copy when the digest is already present
   (https://github.com/mafintosh/content-addressable-blob-store).
2. This vault's own rule for retried writes
   (`knowledge/notes/idempotent-retry-after-quarantine-decision.md`): a key is
   never reused for a different payload, but an unchanged payload must not be
   refused forever.

## Decision

The candidate path already is a content address for "this claim over this
evidence needs review". When a regular file at that path embeds a claim with
the same `id`, `fingerprint` and `evidence`, the review item already exists:
the planner adds no create change for it and reports it as present. A file
that is missing, not regular, unparsable, or names a different claim is not
treated as present, so the create still meets it and refuses exactly as today.

- `ContradictionPipeline.plan_candidate_changes` returns created and present
  candidate paths; `plan_changes` keeps its signature. `_commit` returns the
  present path without a transaction when nothing is left to write.
- A compile batch that is quarantined and whose every candidate is already
  present raises `CandidatesAlreadyQuarantined`; `_apply_batch` reports
  "batch still quarantined" with outcome `quarantined` and records no commit,
  so the daily stays pending as the contract says. New candidates in the same
  batch are still written.

Not changed here, and put to the owner: a pending quarantined daily is planned
by the model again on every compile, which spends tokens each night and keeps
waiting for a manual review the owner does not do (102 candidates in
`knowledge/inbox/claims/` on 2026-09-11).

Files: `scripts/contradiction_pipeline.py`, `scripts/compile_memory.py`,
`tests/test_contradiction_pipeline.py`, `tests/test_compile_transactions.py`,
`CHANGELOG.md`.
