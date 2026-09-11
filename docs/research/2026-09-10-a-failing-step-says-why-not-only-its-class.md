# A failing step says why, not only its class

Date: 2026-09-10. Trigger: audit finding OPS-09 (and the second half of
OPS-17). Five in-process steps report a failure as the exception's class
name and nothing else: the nightly's health report (`health report skipped:
RuntimeError`), three repair paths in `doctor.py` (`Index repair failed:
…`, `Maintenance owner release failed: …`, `Repair failed: …`) and
`self_update._merged_update` (`_outcome("error", type(error).__name__)`).
`RuntimeError` alone does not let the owner act. Subprocess steps already
say why: `maintenance_helpers` logs the redacted stderr head and the
artifact path. `tests/test_scheduled_nightly.py` asserted the class-only
line, locking the defect in.

## Sources

1. This repository: `maintenance_helpers.run_step` reports
   `f"{type(e).__name__}: {redact_secrets(str(e))}"` for OS errors and the
   redacted stderr head for subprocess failures — the shape the in-process
   steps lack; `secret_redact.redact_secrets` is the one redaction boundary
   for text that reaches a log.
2. Rule 3: a report must carry the fact needed to act on it; the class is
   the shape of the failure, the message is the fact.

## Decision

One helper, `secret_redact.describe_error(error)`, returns
`"<Class>: <redacted message>"`; the five sites use it, and the nightly
test asserts the message is present. Nothing else changes.

Files: `scripts/secret_redact.py`, `scripts/scheduled_nightly.py`,
`scripts/doctor.py`, `scripts/self_update.py`, `tests/test_scheduled_nightly.py`,
`tests/test_secret_redact.py`, `CHANGELOG.md`,
`docs/AUDIT-2026-09-10-operations-and-reliability.md`.
