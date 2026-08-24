---
type: decision
status: active
confidence: high
source_authority: user
date: 2026-08-22
---

# Health Findings Resolve Themselves

One-sentence summary: a health finding describes a live condition and returns to
green on its own when that condition ends, so no report ever waits on a human
action.

## Context

The owner set the governing requirement in one sentence: everything must work
automatically, without involving the user. Two findings in this vault
contradicted it on 2026-08-22, and both were the same defect wearing different
clothes — a report that could not go green again by itself.

`transactions` reported `error` for a quarantined compile attempt from
10:45:41Z. The refusal was fixed and the same sources compiled successfully
ninety minutes later, but under a different operation identity, because the fix
changed the dispositions the identity is derived from. The only exemption the
check knew was a committed retry inside the same chain, so that attempt would
have been counted as an open problem for as long as the vault kept its evidence
— which is for ever. No command disposes of quarantine, and none should: it is
retained evidence.

`capture` reported `degraded` for two captures lost on 2026-08-21. Both were
diagnosed and their causes fixed the same day. The counter only went down when
somebody ran `--clear`.

## Decision

**A finding is a statement about now.** Every check must be able to return to
green through the ordinary operation of the system. An operator action may
retire evidence, and never has to.

**A quarantined attempt is history on either of two proofs.** A retry in its own
chain committed — the lineage. Or everything it meant to create was created by a
transaction that committed — the outcome. The second is the ordinary case, and
it is exact: the paths come from the refused attempt's own operations, the
proof from operations of a committed one. An attempt that intended no creation,
or whose pages exist nowhere, stays a finding, because there something really
was lost.

**A lost capture is a live finding for seven days.** The counter keeps the
evidence and is never zeroed automatically. What is bounded is the finding: a
new loss turns the check degraded at once, a quiet week returns it to green, and
the totals stay in the details either way. The same window silences the
SessionStart line, which existed to make a loss noticed and stopped being read
once it could not be answered.

## Why this shape

Alerting systems resolve on the condition, not on the record of it. Prometheus
alerting rules deactivate on the first evaluation where the condition is not
met, and its Alertmanager is deliberately stateless so a resolution survives a
restart. Self-healing practice closes an alert by re-running the same check that
raised it. The failure mode of the opposite choice has a name, alert fatigue,
and the recommendation is always to drop findings that no longer describe live
conditions. Acknowledgement exists to silence an alert that is still true, which
is a different thing from one that has stopped being true.

Neither rule deletes evidence and neither can make a live problem green: both
are conditions evaluated from scratch at every run.

## Evidence

- Live vault before the change: `transactions (error)` with
  `quarantined_unresolved: 1`, `capture (degraded)` with two losses from the
  previous day.
- The refused attempt `a2bd6b02a0e0461b` (10:45:41Z, `dlp_content_blocked`)
  intended to create three receipts; all three were created by the committed
  transaction `7d9ffd698d1e4f2c` at 11:55:54Z.
- After the change the same vault reports `transactions (ok)`. The capture
  finding stays degraded because those losses really are recent, and will clear
  itself.
- Regressions: `test_a_refused_attempt_whose_pages_another_commit_wrote_is_history`,
  `test_a_refused_attempt_whose_pages_nobody_wrote_still_needs_attention`,
  `test_a_loss_that_stopped_happening_returns_the_capture_check_to_green`,
  `test_session_start_stops_naming_a_loss_that_stopped_happening`.
- Research: `docs/research/2026-08-22-health-findings-must-resolve-themselves.md`.

## Open questions

Whether the seven-day capture window should be derived from the capture cadence
of the machine rather than fixed. A vault used once a fortnight returns to green
without ever proving the capture path works again.

## Related

- [[knowledge/notes/observable-capture-and-bounded-maintenance-decision]] — the
  decision that made a lost capture visible in the first place.
- [[knowledge/notes/idempotent-retry-after-quarantine-decision]] — the retry
  chain whose ordinary case this rule recognises.
- [[knowledge/notes/nightly-builds-generation-vectors-decision]] — what the maintenance pass must build so the published generation can answer in any language.
- [[knowledge/notes/automatic-code-update-decision]] — links to this page.
