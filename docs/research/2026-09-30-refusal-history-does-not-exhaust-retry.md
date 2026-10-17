# Refusal history does not exhaust retry

Date: 2026-09-30. Scope: the existing compile retry-ordinal search, without new
paths, schemas, settings keys or services. Implementation starts in the isolated
checkout while the installed nightly compile is still running.

The search gives up after 100 historical quarantined attempts, even when the
cause of refusal has been corrected. The count has no measured basis. Three
regressions reproduce exhaustion and the missing deadline/cancellation boundary.
Evidence: `logs/audit-2026-09-30-continuation-retry-red.txt`.

Primary sources checked today: [Stripe idempotent requests](https://docs.stripe.com/api/idempotent_requests),
[AWS safe retries](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/),
[gRPC deadlines](https://grpc.io/docs/guides/deadlines/).
The first two explain preserving request identity and refusing a different
payload under the same identity; the third explains propagating caller time
budgets and cancellation. They do not require a hundred-record retry ceiling.
No external service or dependency is added.

Select the existing first-free ordinal walk, with deadline/cancellation checked
around every record read. Quarantined rows and parent links stay unchanged; the
first non-quarantined or missing sibling remains the selected identity. The
compile passes its existing whole-operation deadline and cancellation callback.
A standalone caller can provide its own deadline; without one, the existing
writer-wait budget applies. That default is reused, not a new numeric limit.

Raising the counter only postpones the defect. Jumping to the maximum ordinal
could skip a committed or resumable sibling and break idempotency. Deleting or
resetting old refusal rows would lose evidence. The chosen walk keeps constant
local state, can traverse more retained rows, and returns a timeout rather than
pretending the history itself makes an operation permanently impossible.

Qualification requires a chain beyond the former ceiling, real quarantine/receipt
integration, cancellation and caller deadline checks, real complexity/branch
analysis and installation only after the live compile has released its owner.

Verified: **113 tests passed in 39.84 seconds**, including real compile quarantine
and receipt integrations, chains beyond 100, cancellation, late-read refusal,
caller-budget precedence and real complexity/branch checks. Ruff passed. The
three public files were then installed under the quiescence fence after the
nightly service completed with failures=0. Preimages and before/after hashes are
retained. Evidence: `logs/audit-2026-09-30-continuation-retry-green.txt` and
`logs/audit-2026-09-30-continuation-retry-activation.json`.
