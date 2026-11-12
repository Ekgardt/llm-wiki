# Failed capture fence release retains its canonical owner

Date: 2026-10-13. Scope: the shared capture-publication ownership context.

The installed failure trail reported `FOREIGN KEY constraint failed` chained
from `database is locked`. The publication context used nested `finally`
blocks: a failed dependent fence release still triggered deletion of its
canonical owner. The real schema prohibits deleting that owner while the
fence references it. A regression using a real adopted queue, coordinator,
owner and fence reproduces this exact exception chain by injecting the
reported lock error only at fence release. The original implementation fails
with `IntegrityError`, replacing the lock error.

The context now releases the canonical owner only after its dependent fence
release succeeds. If fence acquisition failed, the unreferenced owner is
still released. If fence release failed, both authority records remain for
the existing proof-of-death recovery contract and the original release error
propagates. No exception is suppressed, no row is force-deleted, no new
recovery location or owner policy is added, and no lock wait is extended.
Successful publication and failure before publication continue to release
both records. The shared context covers ordinary capture and breadcrumb
publication; the same ordering therefore protects both consumers.

This establishes and fixes the cleanup-error cause. It does not identify the
live lock holder, prove the payload had already been saved, recover missing
historical events, or qualify all capture loss as repaired. Those remain
separate work. Retained owners are deliberate recovery evidence: deletion
requires the existing expired-and-proven-dead ownership qualification.

Alternatives rejected: deleting the owner despite its foreign key would
invalidate authority; swallowing either error would mask failure; arbitrary
retries would not correct the dependency order; a new atomic release API
would be a broader redesign than this proven ordering defect requires.

Primary sources consulted for this change and the adjacent lock experiment:
- SQLite busy handler and explicit refusal:
  https://www.sqlite.org/c3ref/busy_timeout.html
- CPython sqlite3 transaction context and exception propagation:
  https://docs.python.org/3.13/library/sqlite3.html
- pytest threaded-test isolation and exception visibility:
  https://docs.pytest.org/en/stable/explanation/flaky.html

Regression protects the original lock exception, both retained authority
rows, foreign-key integrity, and cleanup after failed fence acquisition.
Related normal, refused, renewed, queue-link and ownership tests are required.

Related-scenario qualification: capture adoption contained the same duplicated
nested-finally implementation. The expanded regression failed specifically in
that path (one failed, three passed). Adoption now imports the canonical
publication ownership context under its existing local name. Its duplicate
implementation is removed. It obtains the same fresh capture owner and intent
fence, additionally using the existing lease keepalive while registration is
in flight. No retry or lease value changes. Both normal capture and adoption
are covered by the same failed-acquisition and failed-release regressions.
