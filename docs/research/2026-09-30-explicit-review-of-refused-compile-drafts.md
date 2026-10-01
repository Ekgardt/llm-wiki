# Explicit rejection is distinct from successful recovery

Date: 2026-09-30. Local audit completion authorized by the owner.

The live vault has six unapplied refusals for three unique generated pages.
Review of their intact after-images found unsupported outcome claims inferred
from command invocations, and an unsupported conclusion about user authorization.
Replaying them is incorrect. Current diagnostics only recognize later publication
as resolution and offer no way to record the operator's negative decision.

Primary references reviewed:
- [AWS SQS dead-letter queues](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html):
  isolate and inspect failed work before deciding whether to redrive it.
- [Azure Service Bus dead-letter queues](https://learn.microsoft.com/en-us/azure/service-bus-messaging/service-bus-dead-letter-queues):
  operator inspection and explicit settlement differ from blind retries.
- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html):
  retaining the existing transaction and atomic publication primitives avoids
  inventing a second mutable state transition across files and databases.

Decision: an explicit operator command writes a create-only review record beside
an existing transaction plan. It binds the exact transaction/request/plan/error,
records actor, UTC time and rationale, and only accepts quarantined compile
transactions whose operations were never applied. The writer validates all plan
artifacts and holds the existing canonical writer gate. It does not change the
transaction state, publish the draft, mark sources compiled or waive DLP.

Doctor validates the binding and plan, checks that operations remain unapplied,
and reports reviewed rejections separately from unresolved refusals. Missing,
malformed, changed-plan and mismatched reviews leave the finding open. Review
replay is idempotent; changing an existing decision is refused. This is a local
operator record under the same owner-only runtime trust boundary as the other
operational artifacts, not a signature against a malicious local owner.

Alternatives rejected: restoring unsupported notes, editing SQLite statuses,
forging retry lineage, or declaring any later source receipt equivalent to the
refused outputs. None proves that the old semantic claims were correct.

Retention: the original quarantine, after-images, source data and review remain.
GC and run-deletion protections are unchanged. Review does not grant deletion;
those records may be removed only when the existing operational retention and
whole-runtime deletion contract permits it. They are audit evidence, not a second
active implementation. No service, database, runtime root or automatic reviewer
is introduced. Legacy readers will continue reporting the old refusals if rolled
back, which is conservative.

Tests cover real DLP quarantine, missing/changed reviews, changed plan, partial
application, other operation types, replay, retained deletion protection and
absence of rejected output. Before the doctor change, a valid explicit review
still produced one unresolved finding; no import error was used as regression
proof. An initial create-collision fixture produced `conflicted`, so it was
replaced by the actual DLP-refusal path before reproducing the defect.

The existing refused-page restoration command also checks this decision: it
skips explicitly rejected drafts and refuses an invalid review instead of
resurrecting content. A regression proved the unmodified restoration command
still offered a rejected draft, and now refuses to replay it. Quarantine
retention remains protected independently of diagnostic resolution.
