# Compile publication keeps its validated plan while another writer is active

On 2026-10-10, a real manual compilation stopped after 3,707.71 seconds with
`TimeoutError: timed out waiting for the global Markdown writer gate`. It had
already published 32 page paths. The identity of the competing writer at that
historical instant was not recorded and remains unknown.

The code defect is independently reproduced with a real competing writer in both
the legacy and canonical v3 coordinator. `_published_once` used the coordinator's
short default admission window as the lifetime of the whole publication. A normal
busy writer therefore discarded the in-memory, validated publication attempt even
when the caller's whole-call deadline had not expired. The original code fails
both contention regressions; the candidate commits after that same owner leaves.

A live stack also showed a compiler holding the Markdown writer gate while its
post-commit claim-index rebuild repeatedly reads and hashes source files. That
explains why legitimate ownership can exceed a short admission attempt. It does
not identify the historical owner or qualify a separate rebuild optimization.

## Choice and boundaries

Reuse the existing admission attempt window, canonical ownership proof, caller
deadline and cancellation callback. Retry only entry into the writer gate, without
redrafting or reassessing a plan merely because admission was busy. Release through
`ExitStack` on every exit. Only the established writer-admission timeout is retried;
other timeouts, ownership errors, source failures and exceptions inside the entered
gate propagate unchanged. Check deadline and cancellation before each attempt and
again immediately after admission. There is no new attempt cap or runtime contract.

Increasing a fixed wait would only move the failure threshold. Stealing a live
owner would violate fencing. Retrying the whole model call would waste tokens.
Moving the claim rebuild outside the writer gate requires separate consistency
qualification and is not part of this change. A manual caller without a deadline
continues waiting for admission; cancellation is observed between existing bounded
attempts. This preserves the caller's existing unbounded/manual choice.

The distinction between a short contention attempt and a whole-operation budget
is supported by three independent primary sources reviewed on 2026-10-10:
[SQLite busy timeout](https://sqlite.org/c3ref/busy_timeout.html),
[PostgreSQL locking](https://www.postgresql.org/docs/current/explicit-locking.html),
and [Azure retry guidance](https://learn.microsoft.com/en-us/azure/architecture/patterns/retry).
PostgreSQL is comparative locking guidance, not a new product dependency.

## Qualification

The original both-contract regression reports two failures and one cancellation
pass. The candidate related run reports 114 passes. Five admission tests plus five
existing sizing-scope tests pass on Python 3.10. Actual Lizard and AST analysis
measures 25 changed/new callables across this change and the qualified sizing
optimization: maximum CCN 4, at most two `if` statements and nesting at most two.
Tests retain real source bytes, transactional publication and real coordination;
no production fake provider or receipt reset is introduced.

These are component results. A fresh installed full compile, whole-cycle cost,
memory generation, retrieval/citations and complete maintenance health remain
required before point 7 can be closed.
