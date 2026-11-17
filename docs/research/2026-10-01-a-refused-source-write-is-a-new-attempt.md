# A refused source write is a new attempt

Date: 2026-10-01. Status: qualified code correction; installation is recorded separately.

The actual capture-worker diagnostic repeats `duplicate mutation ended in noncommitted state quarantined`. Three current breadcrumb-source transactions are retained with `precondition_failed`. Reusing their deterministic operation identity cannot succeed even when a later worker holds current authority. This is a retry identity defect, not permission to ignore the original precondition.

A real SQLite/Markdown regression prepares a create-only source, changes an independent file named by its precondition, and observes the actual refusal and quarantined transaction. A subsequent source delivery fails on the old code with the same duplicate-mutation error. The first test preparation lacked its required parent directory; that failed preparation is retained separately and is not the defect reproduction.

## Current primary research and alternatives

Sources checked 2026-10-01:

- [AWS Builders Library: Making retries safe with idempotent APIs](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/): bind request identity and effects, retain auditable intent, and distinguish different intent from a duplicate. The article is an established principle, not a claim about a new dependency or release.
- [Microsoft Azure Architecture Center: Retry pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/retry): distinguish transient and permanent errors and verify idempotency under failure.
- [SQLite: Isolation](https://www.sqlite.org/isolation.html): transaction isolation does not establish that an earlier refused logical operation may be treated as committed.

Deleting the refused transaction, accepting a noncommitted duplicate, overwriting an existing source, disabling DLP or ignoring a stale capture fence would lose evidence or authority; these alternatives are rejected. A separate retry mechanism or new schema is unnecessary: MarkdownCoordinator already provides ordinal attempts and parent linkage, used by compile/checkpoint writes.

The candidate uses that existing attempt selection inside the worker's validated writer gate, prepares a create-only MarkdownChange with the current source-absence and capture preconditions, links the refused parent, and applies through the same recoverable transaction boundary. A matching permanent source remains idempotent; a conflicting source still refuses. No automatic terminal-task redrive, new attempt quota, setting, path, schema or daemon is added. Transiently refused attempts can accumulate; existing retained evidence and caller/worker budgets remain authoritative. A new attempt can also fail and must be reported.

## Evidence and remaining qualification

Native code navigation: logs/audit-2026-10-01-breadcrumb-retry-architecture.json. Actual refused-operation metadata: logs/audit-2026-10-01-breadcrumb-refusal-metadata.json. Original failing scenario: logs/audit-2026-10-01-breadcrumb-retry-red-corrected.txt. Candidate qualification and installation are not yet claimed.

Candidate evidence/worker checks: 34 passed. Related source-publication, terminal-proof, redrive and quality checks: 50 passed. Exact public-source qualification: 101 passed; separate real whole-repository branch/complexity guards: 30 passed. Ruff passed; actual Lizard CCN is 2 for the changed creator, 1 for the new commit helper and 2 for the regression. Fresh Gitleaks found zero leaks. The broad hermetic regression exports were frozen before this correction and must not be described as covering it. Existing terminal tasks are not automatically redriven; actual historical completion and unknown-owner recovery are separate unresolved work.

Evidence: logs/audit-2026-10-01-breadcrumb-retry-{green.txt,related.txt,public-tests.txt,public-gitleaks.txt,branch-guards.txt,public-plan.json,cutover.json}.
