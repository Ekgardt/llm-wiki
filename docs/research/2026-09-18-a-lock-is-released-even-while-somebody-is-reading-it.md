# A lock is released even while somebody is reading it

Date: 2026-09-18
Files: `scripts/memory_state.py`,
`tests/test_a_lock_is_released_even_while_somebody_is_reading_it.py`

## What was found

Run 35382495391 still fails two tests on Windows shard 1, on every Python version:

```
tests/test_capture_hooks.py::test_prompt_capture_claim_is_one_reservation_under_concurrency
  assert (54, False) == (1, False)
tests/test_context_noise.py::test_nightly_catchup_claim_is_atomic_and_once_per_date
  memory_state.StateLockTimeout: Could not acquire state lock: C:\Users\runneradmin\...
```

and, on one shard, `test_prompt_counter_is_durable_and_concurrency_safe` and
`test_prompt_counters_are_isolated_across_parallel_sessions` with it.

They are one defect, and it is in the product.

### The chain

`test_prompt_capture_claim_is_one_reservation_under_concurrency` runs 64 calls through 16
threads in **one process**, all claiming the same key, and requires one shared
reservation. On Windows 54 of them came back with a reservation of their own.

`capture_operation.claim_operation` explains the number:

```python
try:
    update(claim.mutate)
except Exception as exc:  # noqa: BLE001 - counted, never silent (audit OPS-21)
    _count_dropped_write(exc)
    return _fallback_operation_id(prefix, key, source_event_id)
```

and `_fallback_operation_id` uses `source_event_id or uuid.uuid4().hex`. The test passes no
source event id, so **every claim whose state write fails mints a fresh unique id**. 54
distinct claims means 54 failed state writes — `StateLockTimeout`, the very exception the
sibling test reports raw.

So both tests say the same thing: the state lock is not being released on Windows.

### Why the lock is not released

`_release_state_lock` closed the descriptor and then removed the file:

```python
try:
    if LOCK_FILE.read_bytes() == payload:
        LOCK_FILE.unlink()
except OSError:
    pass
```

Windows will not delete a file somebody else has open. Microsoft states it plainly:

> The **DeleteFile** function fails if an application attempts to delete a file that has
> other handles open for normal I/O or as a memory-mapped file (**FILE_SHARE_DELETE** must
> have been specified when other handles were opened).

Python's own `open()` does not ask for `FILE_SHARE_DELETE`, and **every waiter polls this
very file**: `_await_lock_turn` calls `_lock_bytes()`, which is `LOCK_FILE.read_bytes()`,
on every turn of its 50 ms loop. With fifteen threads polling, the holder's `unlink`
regularly lands inside a reader's open handle and fails with a sharing violation — and
`except OSError: pass` threw that away.

The lock file then stays on disk naming an owner that is **alive** — this very process.
No staleness rule can retire it: `_named_owner_is_dead` asks whether the recorded process
is gone, and it is not, and `_judge_by_age` waits out a live owner by design. So one
unlucky release wedges the state lock for the rest of the process, every later writer
burns its full timeout, and every capture claim degrades to a unique fallback id.

On POSIX `unlink` never fails for an open file, which is why this is Windows-only and why
the newline fix in `4fd18a66` — which was a real and separate defect — did not end it.

## Decision

The release waits the readers out instead of giving up: the unlink is retried while the
error is one of the transient Windows sharing errors (5, 32, 33 — the same set
`lsp_process` already retries for its lease), bounded by `LOCK_RELEASE_SECONDS`. A reader
holds the file for microseconds, so a two-second bound is a margin of roughly a million
against the window it is racing, while still bounding a hook's worst case well under the
ten-second lock timeout itself.

The unlink now goes through `os.unlink` on the module, so a test can present the operating
system's refusal without patching anything the code under test decides. `FileNotFoundError`
counts as released — somebody else's retirement got there first, and that is the outcome we
wanted.

What is deliberately **not** done: taking the exclusivity off the file's existence and
onto an OS lock (`msvcrt.locking` / `flock`) would remove the race at the root rather than
out-wait it. That is a redesign of the lock, not a CI fix, and it needs the owner.

## Residual risk, stated plainly

If every retry in the window loses the race, the lock still leaks and this process still
wedges. The change makes that overwhelmingly unlikely rather than impossible. The honest
fix for the remaining sliver is the redesign above.

## Sources

- Microsoft Learn, "DeleteFileW function (fileapi.h)", fetched 2026-09-18, Remarks,
  quoted verbatim above.
- `scripts/capture_operation.py`, `claim_operation` and `_fallback_operation_id` — the
  `uuid.uuid4().hex` that turns each failed write into a distinct reservation.
- `scripts/memory_state.py`, `_await_lock_turn` → `_lock_bytes` → `LOCK_FILE.read_bytes()`
  — the readers that block the delete.
- `scripts/lsp_process.py`, `_WINDOWS_LEASE_RETRY_ERRORS` — the same three error numbers,
  already retried for the same reason.
- Jobs 105721706415, 105721706461, 105721706546, 105721706557 and 105721707097 of run
  35382495391.
