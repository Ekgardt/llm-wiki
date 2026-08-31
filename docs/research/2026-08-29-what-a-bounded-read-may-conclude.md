# What a bounded read may conclude

Date: 2026-08-29
Question: when a health check cannot read every row, what may it say, and what
must it still refuse?

## Why this was asked

`scripts/doctor.py` caps every operational scan at `MAX_OPERATIONAL_ROWS =
10_000`. On this vault the `operation` table holds 11,826 rows, so the scan
truncates. Two statements followed, and both were false.

The truncation appended `transaction_state_unknown` to the deletion codes — a
claim that a transaction carries a state nobody recognises. All 9,046
transaction rows carry valid states. The doctor was reporting corruption about
rows it never read.

Worse, and not previously recorded: the transactions whose operations fell past
the ceiling looked like transactions with no operations at all, which
`_transaction_row_corrupt` reads as corrupt. Measured on the live vault: a full
operation read flags **1** transaction; the truncated read flags **1,407**.

Both are permanent, because the tables only grow. That contradicts
`knowledge/notes/self-resolving-health-findings-decision.md`, which requires a
finding to describe a live condition and return to green by itself.

## The structural point

The ceiling is the same constant for both tables. Every transaction that is
neither `preparing` nor `discarded` must own at least one operation — that is
what `_transaction_row_corrupt` asserts — so `operations >= transactions`
always. The operation scan is therefore *guaranteed* to breach the ceiling
before the transaction scan does. The doctor was built so that it must make a
false corruption claim before it can ever make a truthful truncation claim.

## What current practice says

**Nagios separates "could not determine" from "confirmed bad."** The plugin API
defines four exit codes: 0 OK, 1 WARNING, 2 CRITICAL, 3 UNKNOWN. The
guidelines are explicit that UNKNOWN "does not necessarily indicate a problem"
— it says the check could not give a clear, unambiguous status, typically
because the check itself could not run properly. CRITICAL is reserved for a
confirmed bad condition in the monitored resource. A truncated read is
UNKNOWN-shaped, not CRITICAL-shaped.

**XACML keeps Indeterminate distinct from Deny, and still fails closed.** In
XACML 3.0 an evaluation error yields `Indeterminate`, not `Deny`. Under the
deny-overrides combining algorithm an `Indeterminate` never yields `Permit` —
it is conservative — yet it remains a separate decision value, and v3 went
further by splitting it into `Indeterminate{D}`, `Indeterminate{P}` and
`Indeterminate{DP}` precisely so the *reason* survives combination. This is
exactly the shape a deletion gate needs: a read that could not see every row
must refuse the permit, while not being recorded as a proven violation.

**Partial data should be surfaced, never silently passed.** The Prometheus
exporter guidance is that a scrape which cannot gather all its data should fail
the scrape rather than return a partial success, because partial failure is
hard to reason about and alert on. The general monitoring lesson is the same:
what must not happen is the check quietly reporting health it did not verify.

## What follows for this code

Three consumers were tangled into one list.

1. **The deletion gate.** `run/` deletion is gated on `deletion_codes`. A
   bounded read that could not see every row cannot prove the table is safe to
   lose, so it must still refuse. This is the XACML deny-biased Indeterminate.
   It keeps a code — but the code must name the read limit, not allege
   corruption.

2. **The health claim.** `status` says whether the vault is in trouble.
   Ordinary growth past a read ceiling is not trouble. Calling it `error`
   produces a permanently red light, which is the alert-fatigue failure the
   accepted decision was written against.

3. **The corruption judgement.** `_transaction_row_corrupt` infers corruption
   from the *absence* of operation rows. Absence is only evidence when the read
   was complete. An incomplete read must abstain, not accuse. A malformed row
   that *was* read is still positive evidence and still accuses.

The queue check in the same file already has the right shape: `_queue_status`
takes `unknown_state` and `corrupt_metadata` as explicit parameters. The
transaction check was the odd one out — `_transaction_result` reverse-engineered
its severity by string-matching the deletion-code list, which is what let a
gate code silently become a health verdict.

## What this research does not settle

Whether the ceiling should exist at all, or be raised, or be replaced by the
deadline the scan already enforces. The bound is a real safety property and
resizing it is a separate question. What is settled here is only that a
truncation must be named as a truncation.

Nor does it settle the second half of `NEW-140`: a quarantined attempt whose
one missing create can never be re-created. That is a question about what
"something was lost" means, not about bounded reads, and it is recorded
unresolved.

## Sources

- [Nagios Plugin API — return codes](https://assets.nagios.com/downloads/nagioscore/docs/nagioscore/3/en/pluginapi.html)
- [Nagios Plugin Development Guidelines](https://nagios-plugins.org/doc/guidelines.html)
- [States of Hosts and Services — Nagios, 2nd ed.](https://www.oreilly.com/library/view/nagios-2nd-edition/9781593271794/ch04s03.html)
- [Introduction to XACML 3.0 Policies (WSO2)](https://is.docs.wso2.com/en/5.9.0/learn/introduction-to-xacml-3.0-policies/)
- [The Logic of XACML — Extended (arXiv 1110.3706)](https://arxiv.org/pdf/1110.3706)
- [Failing a scrape with the Prometheus Go client — Robust Perception](https://www.robustperception.io/failing-a-scrape-with-the-prometheus-go-client/)
- [Prometheus target scrape status — PromLabs](https://training.promlabs.com/training/monitoring-and-debugging-prometheus/web-status-pages/target-scrape-status/)
