# The supervisor lets the worker finish, and a vanished file does not stop it

Date: 2026-09-25. Audit items B-21 and C-19 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- B-21: `mcp_supervisor.code_fingerprint` lists the server's sources with
  `rglob` and then `stat`s each. A file removed between the two — which is what
  the nightly fast-forward does to a renamed or deleted module — raises
  `FileNotFoundError` in the supervisor, the process that must outlive every
  worker.
- C-19: `_stop_worker` closes the worker's stdin, waits `STOP_SECONDS` = 5 s,
  sends SIGTERM, waits 5 s, then SIGKILL. The worker, once its input closes,
  waits up to `SHUTDOWN_INFERENCE_SECONDS` = 30 s for model inference to reach a
  safe point (`mcp_server._settle_inference`). A reload during inference cuts
  that wait at 5 s: SIGTERM lands mid-settle, which is what C-18 describes as a
  possible crash dump on exit.

## Source

- CWE-367, https://cwe.mitre.org/data/definitions/367.html (fetched
  2026-09-25): "The product checks the state of a resource before using that
  resource, but the resource's state can change between the check and the use".

## Decision

- A source file that is gone by the time it is `stat`ed is fingerprinted as
  gone: the supervisor sees a changed fingerprint and reloads, as it should.
- The graceful wait after closing stdin is `GRACEFUL_STOP_SECONDS` = 35 s, the
  worker's own 30 s plus 5 s to exit; SIGTERM and SIGKILL keep 5 s each. A test
  ties the constant to `mcp_server.SHUTDOWN_INFERENCE_SECONDS`.

## Files

- `scripts/mcp_supervisor.py`
- `tests/test_the_supervisor_lets_the_worker_finish.py`
- `CHANGELOG.md`
