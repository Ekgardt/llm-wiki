# One clock for one idea of idle

Dated 2026-09-18. Merge integration: two agents answered audit finding K-C6 in
parallel worktrees, in opposite directions, and both landed.

Files: `scripts/pyright_session.py`,
`tests/test_an_idle_language_server_is_closed_by_the_next_request.py`,
`tests/test_a_startup_that_ran_out_of_time_is_tried_again_later.py`

## What was found

K-C6 said: nothing closes a language server that nobody is using, so up to four
live Pyright processes survive until capacity eviction or process exit.

- The **process** agent
  (`docs/research/2026-09-18-lsp-code-only-tests-reach-is-wired-or-removed.md`)
  deleted `LspProcess.idle_expired`, `_idle_expired_lsp_process` and
  `_IDLE_SECONDS = 300.0` from `scripts/lsp_process.py`, reasoning that the
  session manager already has an idea of idleness — `_idle_entry` reads
  `session._last_used_monotonic` and `_reserve_lru_idle_locked` evicts the
  least recently used — and that a second notion of the same thing, on another
  object, consulted by nobody, is debt. It wrote that the sweep the audit wants
  is the manager's to make, over `_live_entries_locked`, comparing
  `_last_used_monotonic` against a bound.
- The **session** agent
  (`docs/research/2026-09-17-sess-a-session-that-failed-to-close-or-start-is-tried-again.md`,
  decision 4) built exactly that sweep — `_reap_idle_sessions`, bounded by
  `min(caller deadline, 2 s)`, never touching the session being asked for,
  never failing the caller — but read the clock from the process:
  "The clock is `LspProcess.idle_expired` … rather than a second idea of what
  'idle' means."

Merged, the manager calls a method that no longer exists: five tests fail with
`AttributeError: 'LspProcess' object has no attribute 'idle_expired'`. Both
agents wanted one notion of idleness; they disagreed only about which object
owns the clock.

## Practice on this date

- The two clocks do not measure the same thing.
  `LspProcess.last_used_monotonic` advanced on a protocol request;
  `LanguageServerSession._last_used_monotonic` advances in `_operation()` and on
  every `get()` that answers with the session — which is why `_eviction_rank`
  documents that "a checkout whose start failed keeps answering `get()`, and
  each answer renews its last-used time".
- Keeping the process clock would leave the manager with **two** clocks: the
  session's for capacity eviction, the process's for reaping. That is the shape
  both notes rejected. Keeping the session clock leaves exactly one, read in one
  place, for both questions.
- `CLAUDE.md` binds this slice: "Language servers start lazily inside the owning
  MCP process … session-manager capacity"; and the Stage 2 contract, "There is
  no persistent daemon". A reap that rides on a caller's request under the
  caller's deadline satisfies both, whichever clock it reads — so the clock is
  free to be chosen on the "one mechanism" criterion.
- LSP 3.17, *Shutdown Request*: "The shutdown request is sent from the client to
  the server. It asks the server to shut down, but to not exit … There is a
  separate exit notification that asks the server to exit."
  (<https://github.com/microsoft/language-server-protocol/blob/gh-pages/_includes/messages/3.17/shutdown.md>,
  quoted from the session agent's note of 2026-09-17.) Ending a server is the
  owner's act either way, so the reap belongs to the manager that owns it, not
  to the process object.

## The decision

- **One clock: the session's own last-used instant**, in the session manager.
  `_idle_expired_process` becomes `_idle_expired_session`, which reuses
  `_idle_entry` — the same helper capacity eviction uses, so a session that is
  closing, starting or busy is never reaped — and expires it when
  `now - last_used >= _IDLE_SECONDS`. A session of eviction rank 0 owns no
  server and has nothing to reap, so it is skipped and left to capacity
  eviction, which already prefers it.
- `_IDLE_SECONDS = 300.0` moves to `scripts/pyright_session.py`, beside
  `_IDLE_REAP_SECONDS`, because that is where the only reader now lives. The
  process agent's deletion stands: `scripts/lsp_process.py` keeps no idea of
  idleness.
- Both behaviours are kept whole: an idle server is closed by the next request
  for another key, and a stranded or at-capacity session still gives up its slot
  first. Nothing in `CLAUDE.md` changes; no runtime state, thread or timer is
  added.
- One test setup had to follow the merge. `_start_and_age` set
  `_last_used_monotonic = 0.0` to force the eviction order. Under one clock that
  instant is also "expired", so the reap, which now runs first, closed a session
  the eviction test was about. The neighbours are aged by half the idle limit
  instead: old enough to order the eviction, inside the limit that would reap
  them.
