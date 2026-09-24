# Two post-merge failures on main

Dated 2026-09-24. The owner: «40 упал с ошибкой». PR 40 was merged green; the push
run on `main` after it (run 35941975284) failed one job, and so had the push run
after PR 39 (run 35926589114). Both PR runs had passed on the same code, so both
failures are timing or network, and each names a real gap.

Files: `scripts/pinned_download.py`, `scripts/install_language_server.py`,
`scripts/retrieval_telemetry.py`, `scripts/trace_ingest.py`,
`tests/test_retrieval_telemetry.py`,
`tests/test_a_download_that_resets_is_tried_again.py` (new), `CHANGELOG.md`,
`docs/research/2026-09-24-two-post-merge-failures-on-main.md`.

## What was found

1. **`timing::windows_full::py3.14-s1`**:
   `tests/test_retrieval_telemetry.py::test_concurrent_process_writers_do_not_lose_events`
   — eight processes each record one event into a fresh telemetry database; five
   exited 1 with `sqlite3.OperationalError: database is locked` raised from
   `_ensure_schema`, on `CREATE INDEX IF NOT EXISTS`. The connection has a busy
   timeout (`open_operational_db(path, busy_ms=...)`), and it was not honoured.
   `record_events` runs `_ensure_schema` **before** `begin_immediate`: each
   `CREATE … IF NOT EXISTS` statement reads the schema under a SHARED lock and then
   asks for RESERVED. SQLite documents exactly this as the case where the busy
   handler is skipped: "If SQLite determines that invoking the busy handler could
   result in a deadlock, it will go ahead and return SQLITE_BUSY … Consider a
   scenario where one process is holding a read lock that it is trying to promote
   to a reserved lock and a second process is holding a reserved lock that it is
   trying to promote to an exclusive lock" (sqlite.org, `sqlite3_busy_handler`,
   fetched 2026-09-24). Eight writers on a fresh file make that scenario likely;
   Windows file locking makes it slow enough to hit. `trace_ingest.open_store` has
   the same shape (`_ensure_schema` outside any transaction). The generation
   catalog creates its schema with `executescript`, which cannot sit inside a
   transaction; it is opened under the maintenance fence and is left as is, named
   here.
2. **`timing::focused::lexical-and-typescript`**:
   `scripts/install_language_server.py --profile typescript` ended with
   `install failed: <urlopen error [Errno 104] Connection reset by peer>` — one
   TCP reset during the download of the pinned archive, and the installer made one
   attempt. Pyright's installer is the same: one attempt, and every network error
   becomes `pyright_download_failed`.

## Practice on this date

- A write transaction that may create schema takes the write lock first
  (`BEGIN IMMEDIATE`), so lock waits go through the busy handler instead of the
  deadlock short-cut (the SQLite page above; this project already does it for every
  data write through `reliable_memory.begin_immediate`).
- Transient network failures — connection resets, socket timeouts, HTTP 408, 429
  and 5xx — are retried a bounded number of times with growing waits; permanent
  ones (4xx other than 408/429, a redirect, a digest mismatch) are not (Google
  Cloud Storage, "Retry strategy", fetched 2026-09-24).

## The decisions

1. `record_events` creates the schema under its own `begin_immediate` transaction,
   before the one that writes the batch, so a batch that rolls back leaves the schema
   in place (an existing test relies on that); `trace_ingest.open_store` creates its
   schema inside one too.
   A test asserts, through the connection's trace callback, that `BEGIN IMMEDIATE`
   precedes the first `CREATE` in `record_events`.
2. `pinned_download.retry_transient(operation, deadline=…)` runs an operation up to
   three times, waiting 1 s then 4 s, only for a transient network error and only
   while the wait fits inside the deadline; `is_transient_network_error` names
   the class: `HTTPError` 408, 429, 5xx; any other `URLError`; `ConnectionError`;
   `TimeoutError`. `install_language_server._downloaded` opens the target afresh on
   each attempt (so a partial file is truncated) and goes through it. The Pyright
   installer keeps its single attempt for now: its destination is an owned OS
   handle with no truncate primitive on Windows, so adopting the retry there is
   its own change and is not done blind.
3. No threshold or test is loosened; the eight-writer test stays as it is.

## Cost, by rule 4

One `BEGIN IMMEDIATE` around statements that already ran; up to 5 s of waiting on
a download that would otherwise have failed.

## Sources

- SQLite, "Register A Callback To Handle SQLITE_BUSY Errors" —
  https://www.sqlite.org/c3ref/busy_handler.html — fetched 2026-09-24.
- Google Cloud, "Retry strategy" — https://docs.cloud.google.com/storage/docs/retry-strategy
  — fetched 2026-09-24.
- GitHub Actions runs 35941975284 and 35926589114 on `Ekgardt/llm-wiki`, job logs
  read 2026-09-24.
