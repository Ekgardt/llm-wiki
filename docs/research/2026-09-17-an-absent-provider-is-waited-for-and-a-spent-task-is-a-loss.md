# An absent provider is waited for, and a spent task is a loss

Dated 2026-09-17. Finding C-F7 of the third audit (medium-low; the label reproduced by the audit,
the rest confirmed by reading). The research before the fix.

## What was found

- When the model provider is unavailable the capture worker raises a plain `RuntimeError`, and
  `_process_or_fail` fails the task as `processor_failed` with `retry_after=0`. Every try
  spends one of the task's eight attempts. With ordinary hook traffic waking the worker, an
  outage of about an hour — shorter than one subscription usage window — turns every queued
  capture `dead`.
- The comment beside that call says `retry_after=0` makes the queue "re-claim immediately".
  It does not: the legacy queue takes the larger of its backoff and the stated wait, so `0`
  says nothing, and the adopted queue did not read the field at all (queue finding M4, fixed
  on the queue branch the same day so that a stated wait is honoured).
- `capture_diagnostics` labels every failure of the kind `adapter_capture_worker` as
  `deferred`, the eighth and last one included. `capture_failure_totals` subtracts deferred
  failures, so a capture whose task has just died is never shown at session start. The
  session record itself is safe — it is written before the classifier is asked — but the
  classification and the daily entry wait for someone to run `redrive`, and nothing says so.

## Practice on this date

- A caller that knows how long the other side will be away should say so, and the retrier
  should wait at least that long: "Servers send the "Retry-After" header field to indicate how
  long the user agent ought to wait before making a follow-up request. When sent with a 503
  (Service Unavailable) response, Retry-After indicates how long the service is expected to be
  unavailable to the client" ([RFC 9110, section 10.2.3](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3)).
  Our queue already has the field for exactly this (`QueueFailure.retry_after`).
- A failure is classified by its type, never by its text — the rule this module already
  follows for contention (`is_contention`).
- Blocking the task on a capability is the other standard answer, and the queue has it, but a
  blocked task comes back only through an explicit unblock. An absent provider comes back by
  itself, so a wait is the better fit and needs no operator.

## The decision

- An unavailable provider raises `CaptureProviderUnavailable`. The worker fails such a task as
  `provider_unavailable` with a stated wait of one hour; eight attempts then span most of a
  working day instead of about an hour. Every other failure keeps `processor_failed`, with no
  stated wait, and the false comment goes.
- When the failed attempt was the task's last, the worker raises
  `DurableWorkExhausted` from the real error. `capture_diagnostics` records that one as
  `lost`, so session start shows it; every earlier failure of a worker stays `deferred`.

Files: `scripts/flush_memory.py`, `scripts/capture_diagnostics.py`,
`tests/test_an_absent_provider_is_waited_for.py`
