# A session that failed to close, or failed to start, is tried again by the next caller

Dated 2026-09-17. Findings K-A11, K-B13, K-B15 and the session half of K-C6 of the third
audit. The research before the fix. The owner delegated the decision.

Files: scripts/pyright_session.py, scripts/pyright_profile.py,
tests/test_a_stranded_session_close_is_retried_by_the_next_caller.py,
tests/test_a_startup_that_timed_out_is_tried_again_later.py,
tests/test_the_node_probe_is_asked_once_per_node.py,
tests/test_an_idle_language_server_is_closed_by_the_next_request.py,
tests/test_pyright_session.py

## What was found (each read in the code, and reproduced by the tests named above)

- **A11.** `LanguageServerSession.close` sets `_closing` before it waits. When the close
  then fails (short deadline, `OSError` from the process tree) the flag stays. The manager
  skips such a session in `_idle_entry`, counts it in `_live_entries_locked`, and
  `_wait_for_session_close` only polls the flag. Nobody calls `close` again until
  `close_all`, so the checkout and language are dead for the life of the MCP process and one
  of four slots is lost.
- **B13.** `_claim_startup_locked` refuses a second start once `_startup_attempted` is set
  and nothing is retained. One cold query with a two-second deadline therefore leaves
  `pyright_startup_timeout` on the session for the life of the process.
- **B15.** `manager.get()` calls discovery every time, and discovery spawns `node --version`
  every time (`pyright_profile._probe_node`, also used by `lsp_identity._node_identity`). A
  probe that fails once yields an unqualified identity, and `_admit_get` answers with a
  throwaway session although a healthy one is registered.
- **C6.** Nothing closes an idle server. `LspProcess.idle_expired` has no production caller.

## Practice on this date

- The protocol leaves the process with the client: "The shutdown request is sent from the
  client to the server. It asks the server to shut down, but to not exit (otherwise the
  response might not be delivered correctly to the client). There is a separate exit
  notification that asks the server to exit." (LSP 3.17, Shutdown Request,
  <https://github.com/microsoft/language-server-protocol/blob/gh-pages/_includes/messages/3.17/shutdown.md>,
  fetched today.) A shutdown that did not complete leaves a process only its owner can end,
  so the owner has to try again; the server will not.
- Which failures to retry, and how often: "If the fault indicates that the failure isn't
  transient or is unlikely to be successful if repeated, the application should cancel the
  operation and report an exception." and "in an interactive web application accessing a
  remote service, it's better to fail after a smaller number of retries with only a short
  delay between retry attempts". (Microsoft Azure Architecture Center, *Retry pattern*,
  <https://learn.microsoft.com/en-us/azure/architecture/patterns/retry>, source text fetched
  today from `MicrosoftDocs/architecture-center`, `docs/patterns/retry-content.md`.)
- The contract in `CLAUDE.md`: no persistent daemon; servers start lazily inside the owning
  MCP process; capacity is four. So every retry and every reaping has to ride on a caller
  that is already there, under that caller's deadline.

## The decision

1. **A failed close is remembered as stranded and retried by whoever meets it next.** `close`
   marks the session when it raises. A `get()` for the same key closes it under its own
   deadline instead of polling; at capacity the manager prefers a stranded session over the
   least recently used idle one. `close` is serialized by `_close_lock`, so two callers
   cannot close twice. A retry that fails again raises to that caller and stays stranded.
   The pinning test keeps all its assertions; "until `close_all`" stays true when nobody
   asks in between.
2. **A startup that failed on the clock or the operating system is retried, three times at
   most.** A failure whose degradation code ends in `_startup_timeout`, or an `OSError` that
   is not a `PermissionError`, clears `_startup_attempted` and names an instant before which
   no retry may start: 5 s, then 30 s, then 120 s. The session keeps the count, so no manager
   state is added. Claiming a retry clears the degradation codes, so a start that succeeds
   does not answer with the last one's failure. Identity, protocol and capability failures
   stay terminal, as today. A spent session owns no process, and eviction now takes a session
   that owns no server before a healthy idle one, so a checkout that keeps being asked for and
   keeps failing cannot hold a slot against its neighbours.
3. **The Node probe is asked once per Node.** A successful probe is kept for the Node
   executable it was run on, keyed by path, device, inode, size and mtime, for at most
   300 s (a version-manager shim can change what it runs without changing itself). A failed
   probe is never kept. With the probe cached, a transient spawn failure can no longer
   unqualify a checkout that has a healthy session. Configuration and server digests are
   still read on every `get()`: they are the session key, and a changed `pyrightconfig.json`
   must produce a new session.
4. **Idle servers are closed by the next request.** A server unused for 300 s is closed by
   the next `get()` for another key, at most one per call, bounded by
   `min(caller deadline, 2 s)`; its failure does not fail the caller and leaves a stranded
   session for rule 1. No thread, no timer. The clock is `LspProcess.idle_expired`, which
   already existed with the 300 s limit and no caller -- the manager now makes that one call,
   rather than a second idea of what "idle" means.

## What this costs

One extra `os.stat` per `get()`; at most one server shutdown on a caller's time per `get()`.
