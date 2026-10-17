# A session marker belongs to durable lifecycle delivery

Research date: 2026-10-03. This is a repair of the existing v1 capture and canonical
Markdown transaction paths. It introduces no runtime location, schema, environment
variable, queue kind or persistent service.

## Observed defect and scope

The installed adapter publishes transcript evidence before attempting the project
marker. A live global writer can exceed the marker's seven-second foreground
budget. The worker later keeps the conversation and publishes a no-content
terminal without restoring the marker. Without a transcript, `force_stub` attempts
only the foreground marker and retains no intent for its failure. Original-code
regressions reproduced both omissions (two failures, one heartbeat control pass).
The foreground failure is controlled in these regressions; retained live logs
independently establish the actual writer-deadline failure.

Transcript-present session end requires a marker for external projects other than
the user's home. Missing-transcript events require it only with `force_stub`.
Vault and home sessions remain skips. A queued semantic judgment must not decide
whether required lifecycle metadata survives.

## Primary research and alternatives

* [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html): rollback-mode
  transactions require verified locking and flushed durable state. Reuse the
  installed fenced transaction system rather than add an uncoordinated writer.
* [RFC 9110, idempotence](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2):
  repeats must preserve their intended effect. This is a reasoning analogy for
  local delivery, not a claim that this application uses HTTP for capture.
* [Python 3.10 aware datetime](https://docs.python.org/3.10/library/datetime.html):
  retain the occurrence's explicit timezone and time; a retry's wall clock is not
  the original event time. Python 3.10 remains the product compatibility floor.

Longer foreground waiting alone cannot establish eventual delivery and can exceed
the host timeout. A second queue, new acknowledgment file, or independent marker
daemon duplicates the accepted reliability machinery. Replaying the current
delegate unchanged changes its body and date on each attempt, despite a stable
operation id, and can conflict or duplicate after midnight.

The selected repair shares a marker plan based on immutable v1 event metadata.
The foreground and worker use the same operation id, body and dated path. Both
resolve an agent worktree to its already-supported owning checkout. The
worker projects the marker through the existing CAS append pipeline under its
existing owner before classification and terminal completion. A failed projection
leaves the intent retryable. A foreground acknowledgment continues to describe
only the foreground result.

Missing-transcript forced markers retain explicitly labeled lifecycle metadata
through the existing evidence role field. They must never pretend that metadata
is a supplied conversation, or fabricate a provider response. Ordinary missing
transcript events remain activity heartbeats. The adapter's supported direct tag
delegate must take this same durable route; the standalone hook retains its own
skip and failure behavior.

An old eligible external-project intent without an occurrence timestamp needs an
explicit, evidence-grounded fallback; inventing a time is not an acceptable way to
make its retry pass. No historical loss, source failure, receipt or undo artifact
is deleted by this repair. These changes alone do not close all capture,
compilation or unsupported-limit findings.

## Qualification

Original production behavior: two regression failures and one heartbeat control
pass. The candidate passed the lifecycle regressions, including an actual global
writer held by another process, without timing sleeps. That pressure fixture uses
a short test-only append deadline to reach the same deadline condition quickly;
it does not qualify a changed production timeout. A further worktree regression
caught a mismatch between retained worktree identity and the hook's owning
checkout, and the shared plan now uses the existing ownership rule.

A full isolated adopted-v3 cycle retained identical intent bytes, raw-record
digests and classifier prompt digests before and after. Both ran one identical
controlled provider response and zero real model calls. The original completed
without a marker; the candidate committed the required marker before completion.
Measured cycle times were 0.5323 and 0.4032 seconds. One small sample does not
establish a speed improvement, and controlled-provider evidence does not establish
live model quality or a native lifecycle event.

The installed vault's read-only compatibility inventory found two ready v1
records with occurrence timestamps and 25 dead records without them. Sixteen of
the latter name external roots. Those dead records are not redriven or silently
modified. Their source clocks and recoverability require separate investigation;
the candidate refuses to invent an occurrence time. It must not be called a
completed repair of that historical backlog.

Related capture checks, the repository's complexity/structure guards and Ruff
have passed. Exact final counts, local Lizard callable results and installation
proof remain in the operator's ignored evidence logs. Installation status must
be checked against the fenced cutover and byte-identical installed-guard evidence;
this research document is not an installation acknowledgment. The standalone hook
is retained for its supported direct consumers; dead intents and preimages remain
protected evidence, and their deletion is not part of source cleanup.
