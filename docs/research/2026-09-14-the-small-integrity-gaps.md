# The small integrity gaps

Dated 2026-09-14. Item 2.13 of `docs/AUDIT-2026-09-14-2.md`, four low-severity findings.
The research before the fix.

## What was found

1. **A refused compile overwrites the running one's status.** `compile_memory.main`
   calls `_mark_started` (status `running`, clears `last_compile_error`) *before*
   `_acquire_compile_lock`; when the lock is held by another compile it then writes
   `_mark_finished(..., "error", refusal)`. The compile that actually holds the lock is
   now reported as failed by doctor and session start while it runs.
2. **A lock that exists but is empty.** `maybe_compile._try_claim_lock` creates
   `run/compile.pid` with `O_EXCL` and writes its content afterwards. In that window a
   reader sees an empty file: `_lock_state` calls it "stale (unreadable)", `_clear_lock`
   retires it, and `compile_memory._lock_lines` unlinks an empty lock outright. Two
   compiles can then hold the lock at once.
3. **The prompt counter under a 0.1 s lock timeout.** `user_prompt_capture` increments
   `user_prompt_counts` with `HOOK_STATE_LOCK_TIMEOUT = 0.1`; a busy state lock loses
   that increment. The counter only paces two reminders (every 20 and every 10
   prompts). A lost increment moves a reminder by one prompt; a longer timeout would add
   latency to every prompt. **No change**: the trade-off is the intended one.
4. **Adopted-queue retries have no delay.** `memory_queue._apply_failure_state` (v3)
   sets `available_at = now` for a retryable failure, so a task whose provider is down
   is claimed again at once and spends its eight attempts in seconds. The v2 queue
   waits a full-jitter exponential backoff (`retry_base * 2**(attempts-1)`, capped), and
   the adopted policy has the same constants (`DEFAULTS.retry_base_seconds`,
   `retry_cap_seconds`, enforced by `_require_adopted_retry_policy`).

The code graph: `main` ← the `compile_memory.py` entry; `_try_claim_lock` ←
`maybe_compile` spawn path and `_acquire_compile_lock`; `_apply_failure_state` ← v3
`fail`.

## Practice on this date

- Exponential backoff with full jitter for retries
  ([AWS Architecture Blog, "Exponential Backoff And Jitter"](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/)).
- Create a lock file with its content in one atomic step — write a private temporary
  file, then `link(2)` it to the lock name, which fails if the name exists — so no
  reader ever sees a partial lock (the technique NFS-safe lock files use; `link` is
  atomic and fails with `EEXIST`, [link(2)](https://man7.org/linux/man-pages/man2/link.2.html)).
- A status record belongs to the actor that owns the resource; a refused contender
  records its refusal, not the resource's state.

## The decision

1. `compile_memory.main` takes the lock first. A refused run prints its refusal and
   records it as `last_compile_refused_at`/`last_compile_refused_reason`, leaving
   `last_compile_status` to the compile that holds the lock.
2. `_try_claim_lock` writes the payload to a private staged file and links it to
   `run/compile.pid`; `FileExistsError` means another holder. Where hard links are not
   supported it falls back to the previous `O_EXCL` write.
3. No change (above).
4. The v3 failure path sets `available_at` to `now` plus a full-jitter delay from the
   adopted retry constants for a task that goes back to `ready`; `blocked` and `dead`
   keep `now`.

Files: `scripts/compile_memory.py`, `scripts/maybe_compile.py`, `scripts/memory_queue.py`,
`tests/test_the_small_integrity_gaps.py`, `tests/test_capture_terminal.py` (its crash helper
moves the backoff to the past — the evidence for a backoff is
`docs/research/2026-09-09-capture-work-keeps-its-typed-handler.md`: "Eight rapid attempts can
therefore kill a valid capture"),
`docs/research/2026-09-14-the-small-integrity-gaps.md`.
