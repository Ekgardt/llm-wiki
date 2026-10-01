# Repeated prompts are distinct occurrences

Research and verification date: 2026-09-30.

The native adapter excluded user prompts when assigning missing occurrence IDs.
The same words in a session therefore generated the same event ID indefinitely.
The delegate used that ID for its transaction. After the daily marker search
window, another submission conflicted with the already committed transaction's
different target day. Before that, a new submission could be mistaken for an
already saved event. This is not a transaction-integrity failure.

The sender-to-receiver path is native hook input, normalize_occurrence_event,
canonical capture payload, prompt envelope, claim_operation, append_daily,
and the transaction's request comparison. The graph covers these functions;
source inspection confirms the ID propagation and the daily target comparison.
Private runtime evidence and identifiers remain in the private audit report.

## Sources and alternatives

- [AWS Builders' Library](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/)
  distinguishes identical parameters from identical caller intent and recommends
  an explicit request identifier.
- [Stripe's API contract](https://docs.stripe.com/api/idempotent_requests)
  binds a retry key to the original request parameters and rejects mismatches.
- [Claude Code's hook reference](https://code.claude.com/docs/en/hooks)
  documents prompt and session fields for UserPromptSubmit; consumers cannot
  assume every prompt input carries a distinct upstream occurrence identifier.

Keep supplied identifiers and timestamps. When neither exists, assign one UUID
at the existing outer adapter boundary, as other lifecycle events already do.
Do not add the UUID to the prompt payload: its hash still controls the existing
rate limit. This changes neither storage layout nor dependencies, database
schema, budgets, retention, nor the transaction integrity check.

Rejected: content-only identity (confuses new submissions), ignoring a request
mismatch (conceals lost writes), searching every historical daily log (still
misclassifies a new submission), and a new dedupe store (unnecessary).

Tradeoff: an upstream redelivery without any identifier or timestamp cannot be
distinguished from a fresh submission. Each adapter invocation is an occurrence;
downstream delivery keeps its assigned identity. The existing short content rate
limit remains. This does not promise exactly-once delivery, durable recovery of
every timed-out breadcrumb, or stable rendered timestamps on unfinished retries.

Regression tests cover all three host adapters, explicit identity preservation,
the unchanged content hash, and an actual Markdown append two days after the
first. Before the fix, four tests fail, including OperationBoundElsewhereError
at the same transaction comparison as the real failure. Low-level deterministic
event-envelope construction remains unchanged for existing callers.
