---
type: decision
status: accepted
confidence: high
source_authority: ai-derived
date: 2026-08-29
---

# A Bounded Read Is Not Corruption

One-sentence summary: a scan that hit its read ceiling refuses `run/` deletion
under a code that names the read limit, and never reports corruption about rows
it did not read.

## Context

`scripts/doctor.py` caps every operational scan at `MAX_OPERATIONAL_ROWS =
10_000`. The `operation` table on this vault holds 11,826 rows, so the scan
truncates during ordinary growth.

Two false statements followed, both permanent because the tables only grow.

The truncation appended `transaction_state_unknown` to the deletion codes, and
`_transaction_result` derived its severity by string-matching that list, so a
read limit became a corruption verdict and the check reported `error`. All
9,046 transaction rows carried valid states.

The larger half was not previously recorded. Operations past the ceiling are
never read, so the transactions owning them look like transactions with no
operations, and `_transaction_row_corrupt` reads that as corrupt. Measured on
the live vault: a complete operation read flags **1** transaction, the
truncated read flags **1,407**.

The ceiling is one constant for both tables, and every transaction that is
neither `preparing` nor `discarded` must own at least one operation. So
`operations >= transactions` always, and the operation scan is guaranteed to
breach the ceiling first. The check was built so that it must make a false
corruption claim before it can make a truthful truncation claim.

Both contradict [[knowledge/notes/self-resolving-health-findings-decision]],
which requires a finding to describe a live condition and return to green by
itself.

## Decision

**Three consumers are separated, and each says what it means.**

**The deletion gate still fails closed.** A read that could not see every row
cannot prove the table is safe to lose, so it still refuses deletion — under
`transaction_scan_incomplete`, a code that names the read limit. It no longer
borrows `transaction_state_unknown`, which is a claim about a row that was read.

**Severity is computed from findings, not from the gate list.**
`_transaction_result` reads an explicit `state_invalid` fact set by the two
places that actually detect corruption, instead of string-matching
`deletion_codes`. This is the shape the queue check in the same file already
has, where `_queue_status` takes `unknown_state` and `corrupt_metadata` as
parameters.

**An incomplete read abstains instead of accusing.** When the operation scan
truncated, `_transaction_row_corrupt` receives `None` for the positions and
skips the operation-derived tests. Absence of evidence is only evidence when
the read was complete. A malformed operation row that *was* read still accuses,
and a complete read that finds no operations still accuses.

**A transaction discarded before planning is not corrupt.**
`_promoted_for_recovery` discards straight out of `preparing`, so `plan_hash`
is still the empty string the insert wrote. Only `preparing` was exempt, so
every such row was permanently corrupt metadata. `discarded` is now exempt for
the empty string only; a committed row still needs a real digest.

## Why this shape

Nagios has separated these for decades: exit code 3 UNKNOWN means the check
could not determine the status, and is explicitly not exit code 2 CRITICAL,
which means a confirmed bad condition. A truncated read is UNKNOWN-shaped.

XACML 3.0 is the precedent for the gate half. An evaluation error yields
`Indeterminate`, not `Deny`; under deny-overrides an `Indeterminate` never
yields `Permit`, so it is conservative, yet it stays a distinct value — v3 even
split it three ways so the reason survives. That is exactly a deletion gate
that must refuse on an incomplete read without recording a proven violation.

Prometheus exporter guidance supplies the third constraint: a scrape that
cannot gather all its data must not quietly report success. So the truncation
stays visible in `codes`, in `truncated_scans`, and in the refused deletion
permit. What it no longer does is masquerade as corruption.

## Evidence

- Live vault before: `transactions (error)`, codes `dlp_content_blocked`,
  `precondition_failed`, `transaction_metadata_corrupt`,
  `transaction_operation_scan_truncated`; deletion codes
  `transaction_state_unknown`, `transaction_state_corrupt`,
  `transaction_quarantined`, `transaction_undo_retained`.
- Live vault after: `transactions (error)`, codes `dlp_content_blocked`,
  `precondition_failed`, `transaction_operation_scan_truncated`; deletion codes
  `transaction_scan_incomplete`, `transaction_quarantined`,
  `transaction_undo_retained`; `state_invalid: False`. The remaining `error` is
  `quarantined_unresolved: 1` — a true statement, recorded unresolved below.
- Measured cause: full operation read flags 1 corrupt transaction, truncated
  read flags 1,407; 11,826 operation rows against a 10,000 ceiling.
- The one genuinely corrupt row was `20477c3f50604116ab485468e399788f`,
  `discarded` with an empty `plan_hash` — the fourth defect above, now exempt.
- Regressions: `tests/test_doctor_bounded_scan_truth.py`, nine tests, five
  failing before the change.

## Open questions

Whether `MAX_OPERATIONAL_ROWS` should exist at all for a derived table whose
cardinality is bounded below by the primary table, or whether the deadline the
scan already enforces is the correct bound. Not decided here: resizing a safety
bound is a separate question and needs the owner.

The second half of `NEW-140` is **not** resolved and was deliberately not
relaxed. Quarantined attempt `cb387b9645434586a379f252ac564b8f` intended eight
receipt creates; seven were created by the committed compile `76f1199ba76c` and
exist, one never was. Its refused bytes record `no_durable_content` with empty
`operations` and `evidence`, so nothing was lost — but its name derives from a
14,188-byte slice of a daily file now 332,122 bytes, so no future compile can
create that path and the finding can never clear.

Relaxing `_outcome_was_written` to accept "exists or is provably unreachable"
was rejected **for now, pending the owner**, because every tractable
implementation is worse than the finding. Proving unreachability means
recomputing compile source identities inside the health check — importing the
splitter's semantics and version into the doctor's verdict. The cheap
alternative, exempting everything under `knowledge/daily/receipts/`, is a
blanket exemption for an evidence class, and the accepted decision says in
terms that an attempt "whose pages exist nowhere, stays a finding, because
there something really was lost." Amending that sentence is the owner's call,
not mine.

What leaving it costs: `transactions` stays `error`, so the SessionStart health
block is injected every session with a finding that can never clear — the
alert-fatigue failure the accepted decision exists to prevent. It costs nothing
in deletion: `transaction_quarantined` is appended whenever any quarantine
record exists, so resolving this one finding would not unblock `run/` anyway.

## Source / Evidence

- `docs/research/2026-08-29-what-a-bounded-read-may-conclude.md`
- `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md` → `NEW-140`
- `scripts/doctor.py`, `tests/test_doctor_bounded_scan_truth.py`

## Related

- [[knowledge/notes/self-resolving-health-findings-decision]] — the rule this
  restores for the truncation half and cannot yet restore for the quarantine
  half.
- [[knowledge/notes/idempotent-retry-after-quarantine-decision]] — why a
  quarantine record is immutable evidence and is never disposed.
- [[knowledge/notes/reliable-memory-stage-2]] — the transaction and deletion
  contract this check guards.
