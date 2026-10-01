# Health does not share the model interpreter

Date: 2026-09-29. Installed Python 3.14 and CPU PyTorch 2.13.0; the project
also tests Python 3.10–3.14 on Linux, Windows and macOS.

Cold MCP health repeatedly exceeded its ten-second operation deadline while
the background retrieval warmup was running. On the same vault, direct health
without warmup took 5.87 s; with warmup in the same interpreter it took 10.54 s
and 11.63 s. Executing the same `run_doctor` in a fresh interpreter while the
parent performed the same warmup took 6.83 s including startup (6.63 s inside
doctor). All runs kept the same ten-second absolute deadline and real checks.
These measurements support interpreter isolation; they do not identify every
historical capture failure or prove a universal latency bound under all loads.

## Sources and alternatives

Checked on 2026-09-29:

- [Python asyncio](https://docs.python.org/3/library/asyncio-task.html#asyncio.to_thread)
  explains that ordinary threads do not isolate CPU-bound Python work from the
  GIL. Moving work off the event-loop thread is insufficient isolation here.
- [PyTorch multiprocessing guidance](https://docs.pytorch.org/docs/2.14/notes/multiprocessing.html)
  warns about forking an interpreter with background threads or imports in
  progress. The current documentation resolves to 2.14; this change does not
  upgrade installed 2.13 or depend on a new Torch API. A fresh executable avoids
  inheriting that interpreter state.
- [SQLite locking](https://www.sqlite.org/lockingv3.html) and
  [Microsoft.Data.Sqlite concurrency guidance](https://learn.microsoft.com/en-us/dotnet/standard/data/sqlite/database-errors)
  describe read/write lock contention. The installed databases use DELETE
  journaling by an explicit shared runtime contract. Replacing it with WAL to
  address a cold MCP request would require a separate compatibility migration.

Chosen: invoke the existing doctor CLI with the current Python executable,
configured vault/state roots, UTF-8 JSON, and the unchanged absolute deadline.
The parent waits only for the remaining operation time; subprocess timeout
kills and reaps its child. Native health exit statuses remain health results,
including errors: process separation must not turn a red report green.
The existing MCP worker admission still bounds concurrent requests.

Rejected: increase the timeout, omit history/deletion checks, disable model
warmup, fork the loaded model process, or create a persistent health daemon.
A fresh process adds interpreter startup cost, measured in the comparison,
but requires no new dependency, storage format, scheduler or service. Warmup
and host CPU load can still delay the parent; the installed cold scenario must
be rechecked before calling this issue resolved.

## Verification before installation

The production-path candidate finished in 6.03 s with real model warmup and
returned the existing unhealthy report. The isolation regression failed on the
old in-process path; CLI/IPC tests preserve the exact absolute deadline,
separate vault/state roots, legitimate error exits, and rejection of invalid
responses. The full doctor/MCP sets passed 552 tests (four platform skips),
and deletion/context checks passed 129. A sandbox run first failed because its
virtual `/tmp/.git` invalidated a temporary repository fixture; the unchanged
test passed outside that sandbox. Changed functions measured CCN 1–4.
