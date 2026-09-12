# What the quarantine actually holds

Date: 2026-09-12. Trigger: the owner's decision to re-check the quarantined
claims against their dailies, accept the ones whose quote and digest still
agree, and reject the rest with a stated reason. The first step was to measure
what is in quarantine. The count this report carried — «102 спорных
утверждения» — is wrong, and the correction is below.

## Sources

1. `cache/claims.sqlite3` on the installed vault: the `claim` table holds 88
   rows, every one of them `lifecycle = 'active'`. No claim is quarantined, and
   `claim_index_diagnostic` is empty.
2. `run/markdown-transactions-v3.sqlite3`, table `transaction`: 23 276
   committed, 117 quarantined, 13 discarded. The quarantined rows are
   `precondition_failed` (114) and `dlp_content_blocked` (3).
3. For each quarantined row, its `preconditions_json` names the target and
   `created_at` the moment: 116 of the 117 are followed by a **committed**
   transaction against the same target. The one exception is a lease
   precondition (`project_lease`), not a file.
4. `knowledge/notes/idempotent-retry-after-quarantine-decision.md` and commit
   `cbf52ad` ("a refused append is owed its delta, and gets it as a new
   attempt"): a refused append is retried, and the retry is what commits.

## Decision

Nothing to accept and nothing to reject: the quarantine holds no claim at all,
and every quarantined *transaction* is the record of an attempt whose work
landed through the retry that followed it.

- The 114 `precondition_failed` rows are two writers meeting on one daily: the
  loser's digest no longer matched, it was refused, and the delta was written
  by the next attempt. 98 of them name a daily file; 16 name a lease or the
  claim-tree manifest.
- The 3 `dlp_content_blocked` rows are index and log rebuilds the DLP boundary
  refused. That is the boundary doing its job, not a loss.
- The rows stay. By the `run/` deletion contract a quarantined transaction
  blocks deletion of the runtime root, which is the intended protection, and
  doctor's transactions check is green with them present.

Correction to `docs/REPORT-2026-09-12-what-works-now.md`: the line about 102
disputed claims in quarantine names a number this vault does not have. The
measured state is 0 quarantined claims and 117 quarantined transactions, all
accounted for above.

Files: `docs/REPORT-2026-09-12-what-works-now.md`.
