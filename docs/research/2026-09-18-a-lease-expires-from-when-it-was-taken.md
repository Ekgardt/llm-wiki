# A lease expires from when it was taken, not from when its renewer started

Dated 2026-09-18. Finding Q-L22 of the third audit (operational core).

Files: scripts/lease_renewal.py, scripts/flush_memory.py, scripts/doctor.py,
scripts/operational_ownership.py, scripts/private_vault_backup.py,
scripts/markdown_transaction.py, scripts/memory_queue.py,
tests/test_a_busy_database_is_not_a_lost_lease.py

## What was found

`lease_renewal._Deadline.__init__` sets the first expiry to `monotonic() + lease_seconds`, and
`__init__` runs inside the renewer thread, after the lease row was already written. Every renewer
therefore believes its lease lives a little longer than it does: exactly as long as the gap
between the acquiring transaction committing and the thread reaching that line.

The gap is small — starting a thread and importing the module, measured in milliseconds here —
but the number it corrupts is the one thing this module exists to get right. `_retry_wait`
refuses a retry when `slack() < 0`, where slack is "the time left before a renewal started now
could no longer finish by the expiry". An expiry that is late by *d* means the renewer will
still be retrying for *d* after the fence it is holding has already lapsed for everybody else,
and a second holder that reclaimed the expired lease can be writing at the same moment. The
sign of the error is the unsafe one: too long, never too short.

Every one of the six holders knows the right moment and none of them could say it — the
function took no argument for it.

The finding's second half — `busy_database` treating every `sqlite3.OperationalError` as
transient — is left as it stands, with the reasoning recorded: `OperationalError` covers "disk
I/O error" and "no such table" as well as "database is locked", and Python 3.10, this product's
floor, offers no `sqlite_errorcode` to tell them apart. Retrying the unrecoverable ones costs at
most the lease's own remaining slack, after which the renewer gives up and reports the error it
saw. Narrowing the class by matching the message text is the shortcut this round is removing
elsewhere, not one to add here.

## Practice on this date

This is the rule the module says it follows. Kubernetes' leader election measures the lease from
the observation that set it, never from the loop's own start: `isLeaseValid` answers
`return le.observedTime.Add(time.Second * time.Duration(le.observedRecord.LeaseDurationSeconds)).After(now)`,
where `observedTime` is stamped when the record was actually read or written
([kubernetes/client-go, `tools/leaderelection/leaderelection.go`](https://github.com/kubernetes/client-go/blob/master/tools/leaderelection/leaderelection.go)).
The same file says what that margin is for: "A client needs to wait a full LeaseDuration without
observing a change to the record before it can attempt to take over." A renewer whose clock for
the lease starts later than the write is counting time the other side is already counting down.

## The decision

- `renew_until_stopped` takes a keyword-only `held_since`: the monotonic reading taken by the
  caller at the moment the lease was acquired. It has **no default**. A default would put the
  old, unsafe number back the first time a new holder forgot the argument, and the six holders
  that exist all know the value.
- Each holder takes that reading as close to its acquiring call as it can — in the constructor
  or the context manager that already owns the lease object, before the thread is started — so
  the residual error is the microseconds between the commit and the next statement.
- `_Deadline` keeps everything else: the expiry still moves to `started + lease_seconds` on each
  good renewal, where `started` is read before the attempt.
