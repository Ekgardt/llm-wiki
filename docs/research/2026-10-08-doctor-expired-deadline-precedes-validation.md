# Doctor checks the deadline before adoption validation

Research date: 2026-10-08. Python 3.10 remains supported.

The installed health profile took 74.987 seconds with a 60-second budget. After the main checks exhausted the deadline, the runtime-deletion observation spent another 14.6 seconds validating adoption before checking that same deadline. The source confirms this ordering. This change concerns only work entered after expiry; it does not establish that every individual health operation is bounded.

Use the existing monotonic deadline guard before adoption validation. An expired observation reports unknown state and continues to prohibit deletion. Keep adoption validation, ownership acquisition and snapshot validation unchanged for an available deadline. No new limit, permission, cache, dependency or runtime path is introduced.

Alternatives: increasing the time budget conceals the ordering defect; caching adoption requires independent database identities and security evidence and is a separate change; skipping validation for a non-expired observation weakens safety. Moving the existing guard fixes the observed extra work without any of those tradeoffs. A regression test makes adoption fail if an expired request reaches it, including equality at the deadline. Existing adoption and ownership tests cover requests with time remaining.

Independent primary sources:

- Python 3.10 documents the monotonic clock as unaffected by system clock updates: https://docs.python.org/3.10/library/time.html#time.monotonic
- SQLite documents progress callbacks as the cooperative interruption mechanism for database work; a callback cannot replace a pre-operation deadline check: https://www.sqlite.org/c3ref/progress_handler.html
- Trio documents absolute cancellation deadlines and cooperative checkpoints; it is a comparison, not a proposed dependency: https://trio.readthedocs.io/en/stable/reference-core.html#cancellation-and-timeouts

Qualification remains separate: this correction does not declare the installed health report healthy or the full compile complete.

## The caller deadline also has to reach adoption validation

The installed post-build report on 2026-10-08 certified adoption but exhausted the caller budget before the filesystem and transaction checks. Native navigation and source inspection show that the collector omitted its deadline when calling adoption, and the contention wrapper accepted no caller deadline. A separate causal test shows that the existing retry helper also substituted its 30-second contention window for a longer caller validation deadline.

Forward the existing caller deadline through certification to the complete validator. Keep the existing contention window for retries, and keep the caller deadline for validation work. The complete history and both database contracts are still validated. Deadline exhaustion produces an explicitly incomplete diagnostic and an unknown, non-permitting deletion observation; an earlier unrelated timeout remains an error. Direct callers that omit a deadline retain their existing behavior. Increasing the configured budget or replacing complete validation with writer admission would conceal or weaken the affected contract. No new limit or configuration is introduced. The primary references above apply to this extension.
