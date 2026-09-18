# Code only tests reach is wired in or removed

Dated 2026-09-18 (the work began on 2026-09-17 and the clock rolled over mid-task).
Findings K-C5, K-C6 and K-C7 of the third audit, the parts in the LSP process modules.
The research before the change.

Files: `scripts/lsp_process.py`, `tests/test_lsp_process.py`.

## What was found

- **K-C5, the Reliability-v3 registry arm.** `LspProcess._start_with_v3_candidate`,
  `_claim_startup_ownership`'s registry branch, `_heartbeat_ownership_lease`,
  `_canonical_lsp_fields`, `_registry_lease_held`, `_release_ownership_lease_locked` and
  the coordinator's `ownership_registry` / `ownership_lease` fields are reached only when
  a caller passes `ownership_state_root`. Production never does: the one production entry
  point is `LspProcess.start_configured` (`pyright_session.py:3116`), which has no such
  parameter. One test calls `_start_with_v3_candidate` (`tests/test_lsp_process.py:1079`).
- **K-C6, idle reaping.** `LspProcess.idle_expired`, `_idle_expired_lsp_process` and
  `_IDLE_SECONDS = 300.0` have no caller in `scripts/`; two tests exercise the boundary.
- **K-C7.** The rest of the list — `LspProcess.start` / `_start_lsp_process`,
  `ProcessTree.spawn`, `LspProcess.cancel_all`, `stderr_bytes`, `_stop_heartbeat`,
  `_terminal_failure` — is uncalled by `scripts/` but heavily used by the module's own
  tests: `ProcessTree.spawn` 14 times, `_terminal_failure` 17, `cancel_all` 3,
  `stderr_bytes` 3, `_stop_heartbeat` once, and `LspProcess.start` in nearly every test in
  the file (and in the three test files added by this round's work).

## Practice on this date

- A canonical fenced admission registry does exist in production, and other actors use it:
  `capture_adoption._fresh_capture_authority` takes `registry.acquire("capture", …)`, and
  `doctor` takes `"runtime-deletion-check"` and the maintenance owner through
  `OwnershipRegistry._from_adopted_database`. So the LSP arm is not waiting on a registry
  that does not exist — it is not wired to the one that does.
- But `CLAUDE.md` puts that wiring in the future tense: "**Approved Reliability v3 target
  (not implemented)** … All operational actors would use one canonical fenced admission
  registry, including capture, project/Markdown writers, queue, and LSP." It also says the
  implemented LSP slice "adds no Serena runtime dependency, Rust rewrite, second graph,
  catalog, active pointer, runtime root, persistent daemon, or MCP tool". Making the LSP a
  registry actor today would mean threading a state root through
  `pyright_session.py` — another area's file — and giving the LSP slice a second piece of
  operational state. That is an architectural change, not small wiring.
- The session manager already has its own idea of idleness and acts on it:
  `pyright_session._idle_entry` reads `session._last_used_monotonic`, skips sessions that
  are closing, starting or busy, and `_reserve_lru_idle_locked` evicts the
  least-recently-used one when capacity is reached. `LspProcess.idle_expired` is a second
  notion of the same thing, on a different object, that nobody consults. Two answers to
  one question is the shape the owner has rejected before.
- A test-only entry point is not automatically dead code. `LspProcess.start` is the
  unconfigured start the whole suite drives the lifecycle through, and `ProcessTree.spawn`
  is the same for the process tree. Removing them would mean rewriting dozens of tests
  that cover real behaviour, to reach the same code by a longer road.

## The decision

1. **Delete the v3 registry arm** with its test. It is unreachable from production, the
   contract that would make it reachable is explicitly not implemented, and wiring it
   would be an architectural change in another area's file. Nothing in `CLAUDE.md` needs
   correcting: it already describes the registry for all actors in the future tense.
2. **Delete the idle reaping** with its tests. The manager's own LRU eviction is the one
   implementation of "this session has been idle"; a second one, unconsulted, is debt.
   What the audit's finding is really about — up to four servers alive until capacity
   forces an eviction — is a decision for the session manager, and the one call it would
   need is a sweep over `_live_entries_locked` in `get()`, closing entries whose
   `_last_used_monotonic` is older than a bound. That is recorded here and reported to the
   owner; it is not implemented behind the manager's back.
3. **Keep the rest of K-C7** as what they are: the test seams this module is driven by.
   The counts are above, so the next audit need not re-derive them.

Rejected: wiring the LSP into `OwnershipRegistry` today (an architectural change, and the
contract says v3 is not implemented); deleting `LspProcess.start` and `ProcessTree.spawn`
(they are how the suite reaches the lifecycle).
