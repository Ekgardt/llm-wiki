# An unanswered turn remains pending across finite runs

Research date: 2026-10-03. Installation state is recorded separately in the
fenced-install and installed-guard evidence below.

## Reproduced cause

The fact-key extractor permanently excluded an unanswered turn after three
nonempty replies omitted it. The original words remain searchable, but the
requested fact-key and ledger extraction cannot recover when a later provider
does answer. Seven isolated qualification cases fail against unchanged code:
attempt counts of three, four and one hundred; recovery on a later complete
reply; one-pass retry behavior; fresh-turn priority under the existing deadline;
and reporting uncovered attempts without claiming they were completed.

The installed store contains zero key and attempt rows at the preceding
read-only check. This is a reproduced product defect, not a claim that real
keys were recovered from this vault. Native JSON capture recognition remains
an independently diagnosed limitation and is not fixed here.

## Primary research and alternatives

Sources checked on the research date:

- [Google Cloud retry strategy](https://docs.cloud.google.com/storage/docs/retry-strategy)
  distinguishes retryability, idempotency and per-operation time control. Its
  Go client can bound retries by the controlling context without a lifetime
  attempt count. The product does not adopt that client or its numeric defaults.
- [Microsoft's retry pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/retry)
  discusses transient versus lasting failures, retry cost and avoiding immediate
  repeated attempts. An omitted model answer does not prove permanent failure;
  this is a design comparison, not an HTTP error classification.
- [Linux's CFS scheduling design](https://docs.kernel.org/scheduler/sched-design-CFS.html)
  describes serving the least-served runnable task. The document itself notes
  that CFS is making room for EEVDF. Only the scheduling principle is relevant:
  retain this product's existing stable least-asked ordering, without importing
  an OS scheduler, dependency, sleep, or new queue.

Raising three to another number has no measured justification. A configurable
lifetime cutoff would still hide unresolved turns when reached and add a new
policy. Immediate repeated calls would spend the current run's budget without
giving other work a chance. The selected bounded change removes lifetime
retirement and preserves the existing finite snapshot pass, stable attempt
ordering and caller deadline. A batch is visited once per run; later runs can
recover omitted turns. A continually growing supply of fresh turns can still
delay heavily attempted turns; no starvation-free promise is made.

Python compatibility remains 3.10+, tested with the installed 3.12.3. The SQLite
schema, paths, transaction behavior and sole generation reader remain unchanged.
No new environment variable, service, runtime directory or numeric budget is
introduced. The existing batch size and other unrelated extraction limits are
not justified by this change and remain separate audit work.

## Behavior and cost qualification

Attempt rows remain scheduling evidence. `uncovered()` counts attempted spans
without covered answers, and `--status` reports `uncovered`, replacing the
misleading `given up` label. Covered answers, including explicit empty lists,
still complete a turn; a missing provider reply still spends no attempt.
Historical attempts above three are automatically eligible while their source
turn exists. No stored data is deleted or fabricated.

The paired full cycle uses identical synthetic source and controlled replies:
three omitted answers followed by a complete fourth answer. The old policy
makes three calls, reports no waiting work, and produces zero keys or search
hits. The new policy makes the fourth call, persists one key, and returns one
hit whose reader contains the original source words. Prompt plus system bytes
increase from 8,874 to 11,832; this additional work produces the previously
missing result. Measured local cycle times are 0.06894 and 0.05338 seconds; this
is not a speed claim. There are zero real model calls and no billed-token claim.
The first paired driver mistakenly modeled an explicit empty covered answer as
an omitted answer, and then expected a retired baseline turn to remain pending.
Both driver assertions were corrected to match the documented product behavior;
the original seven red regressions remain genuine and unchanged in purpose.

This changes the requested retry policy explicitly. The old regression asserting
retirement is updated to require retained pending work; it is not disabled.
New genuine-before/after guards additionally check useful later recovery,
finite calls, fresh-turn priority and deadline behavior. Full-cycle controlled
collection, key persistence, generation search and original reader qualification
is required before installation. Controlled provider responses cannot establish
real-model quality or billed token efficiency. New recurring retries can cost
more than abandoning work; the existing per-run budget controls admission,
without claiming that it cancels an in-flight provider call.

## Cleanup and evidence

Remove the unused lifetime constant and `given_up` query; retain the attempts
table because it drives scheduling. The September 17 research remains dated
historical evidence with an explicit superseding policy note. Installation must
retain verified preimages and qualify installed imports in an isolated test
directory. Refresh built-in code navigation after installation.

Evidence: `logs/audit-2026-10-03-fact-key-retry-design-architecture.json`;
`logs/audit-2026-10-03-fact-key-retained-retries-red.txt`;
`logs/audit-2026-10-03-fact-key-retained-retries-paired.json`;
`logs/audit-2026-10-03-fact-key-retained-retries-complexity.json`;
`logs/audit-2026-10-03-fact-key-retained-retries-admitted-install-plan.json`;
`logs/audit-2026-10-03-fact-key-retained-retries-installed-guards.json`;
`tests/test_an_unanswered_turn_keeps_its_chance.py`.
